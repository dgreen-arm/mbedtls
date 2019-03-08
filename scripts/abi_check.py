#!/usr/bin/env python3
"""
This file is part of Mbed TLS (https://tls.mbed.org)

Copyright (c) 2018, Arm Limited, All Rights Reserved

Purpose

This script is a small wrapper around the abi-compliance-checker and
abi-dumper tools, applying them to compare the ABI and API of the library
files from two different Git revisions within an Mbed TLS repository.
The results of the comparison are either formatted as HTML and stored at
a configurable location, or are given as a brief list of problems.
Returns 0 on success, 1 on ABI/API non-compliance, and 2 if there is an error
while running the script. Note: must be run from Mbed TLS root.
"""

import os
import sys
import traceback
import shutil
import subprocess
import argparse
import logging
import tempfile
import fnmatch

import xml.etree.ElementTree as ET


class AbiChecker(object):

    def __init__(self, report_dir, old_repo, old_rev, old_crypto_rev,
                 old_crypto_repo, new_repo, new_rev, new_crypto_rev,
                 new_crypto_repo, keep_all_reports, brief, skip_file=None):
        self.repo_path = "."
        self.log = None
        self.setup_logger()
        self.report_dir = os.path.abspath(report_dir)
        self.keep_all_reports = keep_all_reports
        self.can_remove_report_dir = not (os.path.isdir(self.report_dir) or
                                          keep_all_reports)
        self.old_repo = old_repo
        self.old_rev = old_rev
        self.old_crypto_rev = old_crypto_rev
        self.old_crypto_repo = old_crypto_repo
        self.new_repo = new_repo
        self.new_rev = new_rev
        self.new_crypto_rev = new_crypto_rev
        self.new_crypto_repo = new_crypto_repo
        self.skip_file = skip_file
        self.brief = brief
        self.mbedtls_modules = {"old": {}, "new": {}}
        self.old_dumps = {}
        self.new_dumps = {}
        self.git_command = "git"
        self.make_command = "make"

    def check_repo_path(self):
        current_dir = os.path.realpath('.')
        root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        if current_dir != root_dir:
            raise Exception("Must be run from Mbed TLS root")

    def setup_logger(self):
        self.log = logging.getLogger()
        self.log.setLevel(logging.INFO)
        self.log.addHandler(logging.StreamHandler())

    def check_abi_tools_are_installed(self):
        for command in ["abi-dumper", "abi-compliance-checker"]:
            if not shutil.which(command):
                raise Exception("{} not installed, aborting".format(command))

    def get_clean_worktree_for_git_revision(self, remote_repo, git_rev):
        git_worktree_path = tempfile.mkdtemp()
        if remote_repo:
            self.log.info(
                "Checking out git worktree for revision {} from {}".format(
                    git_rev, remote_repo
                )
            )
            fetch_process = subprocess.Popen(
                [self.git_command, "fetch", remote_repo, git_rev],
                cwd=self.repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            fetch_output, _ = fetch_process.communicate()
            self.log.info(fetch_output.decode("utf-8"))
            if fetch_process.returncode != 0:
                raise Exception("Fetching revision failed, aborting")
            worktree_rev = "FETCH_HEAD"
        else:
            self.log.info(
                "Checking out git worktree for revision {}".format(git_rev)
            )
            worktree_rev = git_rev
        worktree_process = subprocess.Popen(
            [self.git_command, "worktree", "add", "--detach",
             git_worktree_path, worktree_rev],
            cwd=self.repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        worktree_output, _ = worktree_process.communicate()
        self.log.info(worktree_output.decode("utf-8"))
        if worktree_process.returncode != 0:
            raise Exception("Checking out worktree failed, aborting")
        return git_worktree_path

    def update_git_submodules(self, git_worktree_path, crypto_repo,
                              crypto_rev):
        process = subprocess.Popen(
            [self.git_command, "submodule", "update", "--init", '--recursive'],
            cwd=git_worktree_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        output, _ = process.communicate()
        self.log.info(output.decode("utf-8"))
        if process.returncode != 0:
            raise Exception("git submodule update failed, aborting")
        if not (os.path.exists(os.path.join(git_worktree_path, "crypto"))
                and crypto_rev):
            return

        if crypto_repo:
            shutil.rmtree(os.path.join(git_worktree_path, "crypto"))
            clone_process = subprocess.Popen(
                [self.git_command, "clone", crypto_repo,
                 "--branch", crypto_rev, "crypto"],
                cwd=git_worktree_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            clone_output, _ = clone_process.communicate()
            self.log.info(clone_output.decode("utf-8"))
            if clone_process.returncode != 0:
                raise Exception("git clone failed, aborting")
        else:
            checkout_process = subprocess.Popen(
                [self.git_command, "checkout", crypto_rev],
                cwd=os.path.join(git_worktree_path, "crypto"),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            checkout_output, _ = checkout_process.communicate()
            self.log.info(checkout_output.decode("utf-8"))
            if checkout_process.returncode != 0:
                raise Exception("git checkout failed, aborting")

    def build_shared_libraries(self, git_worktree_path, version):
        my_environment = os.environ.copy()
        my_environment["CFLAGS"] = "-g -Og"
        my_environment["SHARED"] = "1"
        my_environment["USE_CRYPTO_SUBMODULE"] = "1"
        make_process = subprocess.Popen(
            [self.make_command, "lib"],
            env=my_environment,
            cwd=git_worktree_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        make_output, _ = make_process.communicate()
        self.log.info(make_output.decode("utf-8"))
        for root, dirs, files in os.walk(git_worktree_path):
            for file in fnmatch.filter(files, "*.so"):
                self.mbedtls_modules[version][os.path.splitext(file)[0]] = (
                    os.path.join(root, file)
                )
        if make_process.returncode != 0:
            raise Exception("make failed, aborting")

    def get_abi_dumps_from_shared_libraries(self, git_ref, git_worktree_path,
                                            version):
        abi_dumps = {}
        for mbed_module, module_path in self.mbedtls_modules[version].items():
            output_path = os.path.join(
                self.report_dir, version, "{}-{}.dump".format(
                    mbed_module, git_ref
                )
            )
            abi_dump_command = [
                "abi-dumper",
                module_path,
                "-o", output_path,
                "-lver", git_ref
            ]
            abi_dump_process = subprocess.Popen(
                abi_dump_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            abi_dump_output, _ = abi_dump_process.communicate()
            self.log.info(abi_dump_output.decode("utf-8"))
            if abi_dump_process.returncode != 0:
                raise Exception("abi-dumper failed, aborting")
            abi_dumps[mbed_module] = output_path
        return abi_dumps

    def cleanup_worktree(self, git_worktree_path):
        shutil.rmtree(git_worktree_path)
        worktree_process = subprocess.Popen(
            [self.git_command, "worktree", "prune"],
            cwd=self.repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        worktree_output, _ = worktree_process.communicate()
        self.log.info(worktree_output.decode("utf-8"))
        if worktree_process.returncode != 0:
            raise Exception("Worktree cleanup failed, aborting")

    def get_abi_dump_for_ref(self, remote_repo, git_rev, crypto_repo,
                             crypto_rev, version):
        git_worktree_path = self.get_clean_worktree_for_git_revision(
            remote_repo, git_rev
        )
        self.update_git_submodules(git_worktree_path, crypto_repo, crypto_rev)
        self.build_shared_libraries(git_worktree_path, version)
        abi_dumps = self.get_abi_dumps_from_shared_libraries(
            git_rev, git_worktree_path, version
        )
        self.cleanup_worktree(git_worktree_path)
        return abi_dumps

    def remove_children_with_tag(self, parent, tag):
        children = parent.getchildren()
        for child in children:
            if child.tag == tag:
                parent.remove(child)
            else:
                self.remove_children_with_tag(child, tag)

    def remove_extra_detail_from_report(self, report_root):
        for tag in ['test_info', 'test_results', 'problem_summary',
                'added_symbols', 'removed_symbols', 'affected']:
            self.remove_children_with_tag(report_root, tag)

        for report in report_root:
            for problems in report.getchildren()[:]:
                if not problems.getchildren():
                    report.remove(problems)

    def get_abi_compatibility_report(self):
        compatibility_report = ""
        compliance_return_code = 0
        shared_modules = list(set(self.mbedtls_modules["old"].keys()) &
                              set(self.mbedtls_modules["new"].keys()))
        for mbed_module in shared_modules:
            output_path = os.path.join(
                self.report_dir, "{}-{}-{}.html".format(
                    mbed_module, self.old_rev, self.new_rev
                )
            )
            abi_compliance_command = [
                "abi-compliance-checker",
                "-l", mbed_module,
                "-old", self.old_dumps[mbed_module],
                "-new", self.new_dumps[mbed_module],
                "-strict",
                "-report-path", output_path,
            ]
            if self.skip_file:
                abi_compliance_command += ["-skip-symbols", self.skip_file,
                                           "-skip-types", self.skip_file]
            if self.brief:
                abi_compliance_command += ["-report-format", "xml",
                                           "-stdout"]
            abi_compliance_process = subprocess.Popen(
                abi_compliance_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            abi_compliance_output, _ = abi_compliance_process.communicate()
            if abi_compliance_process.returncode == 0:
                compatibility_report += (
                    "No compatibility issues for {}\n".format(mbed_module)
                )
                if not (self.keep_all_reports or self.brief):
                    os.remove(output_path)
            elif abi_compliance_process.returncode == 1:
                if self.brief:
                    self.log.info(
                        "Compatibility issues found for {}".format(mbed_module)
                    )
                    report_root = ET.fromstring(abi_compliance_output.decode("utf-8"))
                    self.remove_extra_detail_from_report(report_root)
                    self.log.info(ET.tostring(report_root).decode("utf-8"))
                else:
                    compliance_return_code = 1
                    self.can_remove_report_dir = False
                    compatibility_report += (
                        "Compatibility issues found for {}, "
                        "for details see {}\n".format(mbed_module, output_path)
                    )
            else:
                raise Exception(
                    "abi-compliance-checker failed with a return code of {},"
                    " aborting".format(abi_compliance_process.returncode)
                )
            os.remove(self.old_dumps[mbed_module])
            os.remove(self.new_dumps[mbed_module])
        if self.can_remove_report_dir:
            os.rmdir(self.report_dir)
        self.log.info(compatibility_report)
        return compliance_return_code

    def check_for_abi_changes(self):
        self.check_repo_path()
        self.check_abi_tools_are_installed()
        self.old_dumps = self.get_abi_dump_for_ref(self.old_repo, self.old_rev,
                                                   self.old_crypto_repo,
                                                   self.old_crypto_rev, "old")
        self.new_dumps = self.get_abi_dump_for_ref(self.new_repo, self.new_rev,
                                                   self.new_crypto_repo,
                                                   self.new_crypto_rev, "new")
        return self.get_abi_compatibility_report()


def run_main():
    try:
        parser = argparse.ArgumentParser(
            description=(
                """This script is a small wrapper around the
                abi-compliance-checker and abi-dumper tools, applying them
                to compare the ABI and API of the library files from two
                different Git revisions within an Mbed TLS repository.
                The results of the comparison are either formatted as HTML and
                stored at a configurable location, or are given as a brief list
                of problems. Returns 0 on success, 1 on ABI/API non-compliance,
                and 2 if there is an error while running the script.
                Note: must be run from Mbed TLS root."""
            )
        )
        parser.add_argument(
            "-r", "--report-dir", type=str, default="reports",
            help="directory where reports are stored, default is reports",
        )
        parser.add_argument(
            "-k", "--keep-all-reports", action="store_true",
            help="keep all reports, even if there are no compatibility issues",
        )
        parser.add_argument(
            "-o", "--old-rev", type=str, help="revision for old version.",
            required=True,
        )
        parser.add_argument(
            "-or", "--old-repo", type=str, help="repository for old version."
        )
        parser.add_argument(
            "-oc", "--old-crypto-rev", type=str,
            help="revision for old crypto submodule."
        )
        parser.add_argument(
            "-ocr", "--old-crypto-repo", type=str,
            help="repository for old crypto submodule."
        )
        parser.add_argument(
            "-n", "--new-rev", type=str, help="revision for new version",
            required=True,
        )
        parser.add_argument(
            "-nr", "--new-repo", type=str, help="repository for new version."
        )
        parser.add_argument(
            "-nc", "--new-crypto-rev", type=str,
            help="revision for new crypto version"
        )
        parser.add_argument(
            "-ncr", "--new-crypto-repo", type=str,
            help="repository for new crypto submodule."
        )
        parser.add_argument(
            "-s", "--skip-file", type=str,
            help="path to file containing symbols and types to skip"
        )
        parser.add_argument(
            "-b", "--brief", action="store_true",
            help="output only the list of issues to stdout, instead of a full report",
        )
        abi_args = parser.parse_args()
        abi_check = AbiChecker(
            abi_args.report_dir, abi_args.old_repo, abi_args.old_rev,
            abi_args.old_crypto_rev, abi_args.old_crypto_repo,
            abi_args.new_repo, abi_args.new_rev, abi_args.new_crypto_rev,
            abi_args.new_crypto_repo, abi_args.keep_all_reports,
            abi_args.brief, abi_args.skip_file
        )
        return_code = abi_check.check_for_abi_changes()
        sys.exit(return_code)
    except Exception:
        traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    run_main()
