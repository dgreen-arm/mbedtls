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


class RepoVersion(object):

    def __init__(self, version, repository, revision,
                 crypto_repository, crypto_revision):
        self.version = version
        self.repository = repository
        self.revision = revision
        self.crypto_repository = crypto_repository
        self.crypto_revision = crypto_revision
        self.abi_dumps = {}
        self.modules = {}


class AbiChecker(object):

    def __init__(self, verbose, old_version, new_version, report_dir,
                 keep_all_reports, brief, skip_file=None):
        self.repo_path = "."
        self.log = None
        self.verbose = verbose
        self._setup_logger()
        self.report_dir = os.path.abspath(report_dir)
        self.keep_all_reports = keep_all_reports
        self.can_remove_report_dir = not (os.path.isdir(self.report_dir) or
                                          keep_all_reports)
        self.old_version = old_version
        self.new_version = new_version
        self.skip_file = skip_file
        self.brief = brief
        self.git_command = "git"
        self.make_command = "make"

    def _check_repo_path(self):
        current_dir = os.path.realpath('.')
        root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        if current_dir != root_dir:
            raise Exception("Must be run from Mbed TLS root")

    def _setup_logger(self):
        self.log = logging.getLogger()
        if self.verbose:
            self.log.setLevel(logging.DEBUG)
        else:
            self.log.setLevel(logging.INFO)
        self.log.addHandler(logging.StreamHandler())

    def _check_abi_tools_are_installed(self):
        for command in ["abi-dumper", "abi-compliance-checker"]:
            if not shutil.which(command):
                raise Exception("{} not installed, aborting".format(command))

    def _get_clean_worktree_for_git_revision(self, version):
        git_worktree_path = tempfile.mkdtemp()
        if version.repository:
            self.log.debug(
                "Checking out git worktree for revision {} from {}".format(
                    version.revision, version.repository
                )
            )
            fetch_process = subprocess.Popen(
                [self.git_command, "fetch",
                 version.repository, version.revision],
                cwd=self.repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            fetch_output, _ = fetch_process.communicate()
            self.log.debug(fetch_output.decode("utf-8"))
            if fetch_process.returncode != 0:
                raise Exception("Fetching revision failed, aborting")
            worktree_rev = "FETCH_HEAD"
        else:
            self.log.debug("Checking out git worktree for revision {}".format(
                version.revision
            ))
            worktree_rev = version.revision
        worktree_process = subprocess.Popen(
            [self.git_command, "worktree", "add", "--detach",
             git_worktree_path, worktree_rev],
            cwd=self.repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        worktree_output, _ = worktree_process.communicate()
        self.log.debug(worktree_output.decode("utf-8"))
        if worktree_process.returncode != 0:
            raise Exception("Checking out worktree failed, aborting")
        return git_worktree_path

    def _update_git_submodules(self, git_worktree_path, version):
        process = subprocess.Popen(
            [self.git_command, "submodule", "update", "--init", '--recursive'],
            cwd=git_worktree_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        output, _ = process.communicate()
        self.log.debug(output.decode("utf-8"))
        if process.returncode != 0:
            raise Exception("git submodule update failed, aborting")
        if not (os.path.exists(os.path.join(git_worktree_path, "crypto"))
                and version.crypto_revision):
            return

        if version.crypto_repository:
            fetch_process = subprocess.Popen(
                [self.git_command, "fetch", version.crypto_repository,
                 version.crypto_revision],
                cwd=os.path.join(git_worktree_path, "crypto"),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            fetch_output, _ = fetch_process.communicate()
            self.log.debug(fetch_output.decode("utf-8"))
            if fetch_process.returncode != 0:
                raise Exception("git fetch failed, aborting")
            crypto_rev = "FETCH_HEAD"
        else:
            crypto_rev = version.crypto_revision

        checkout_process = subprocess.Popen(
            [self.git_command, "checkout", crypto_rev],
            cwd=os.path.join(git_worktree_path, "crypto"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        checkout_output, _ = checkout_process.communicate()
        self.log.debug(checkout_output.decode("utf-8"))
        if checkout_process.returncode != 0:
            raise Exception("git checkout failed, aborting")

    def _build_shared_libraries(self, git_worktree_path, version):
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
        self.log.debug(make_output.decode("utf-8"))
        for root, dirs, files in os.walk(git_worktree_path):
            for file in fnmatch.filter(files, "*.so"):
                version.modules[os.path.splitext(file)[0]] = (
                    os.path.join(root, file)
                )
        if make_process.returncode != 0:
            raise Exception("make failed, aborting")

    def _get_abi_dumps_from_shared_libraries(self, git_worktree_path,
                                             version):
        for mbed_module, module_path in version.modules.items():
            output_path = os.path.join(
                self.report_dir, version.version, "{}-{}.dump".format(
                    mbed_module, version.revision
                )
            )
            abi_dump_command = [
                "abi-dumper",
                module_path,
                "-o", output_path,
                "-lver", version.revision
            ]
            abi_dump_process = subprocess.Popen(
                abi_dump_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            abi_dump_output, _ = abi_dump_process.communicate()
            self.log.debug(abi_dump_output.decode("utf-8"))
            if abi_dump_process.returncode != 0:
                raise Exception("abi-dumper failed, aborting")
            version.abi_dumps[mbed_module] = output_path

    def _cleanup_worktree(self, git_worktree_path):
        shutil.rmtree(git_worktree_path)
        worktree_process = subprocess.Popen(
            [self.git_command, "worktree", "prune"],
            cwd=self.repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        worktree_output, _ = worktree_process.communicate()
        self.log.debug(worktree_output.decode("utf-8"))
        if worktree_process.returncode != 0:
            raise Exception("Worktree cleanup failed, aborting")

    def _get_abi_dump_for_ref(self, version):
        git_worktree_path = self._get_clean_worktree_for_git_revision(version)
        self._update_git_submodules(git_worktree_path, version)
        self._build_shared_libraries(git_worktree_path, version)
        self._get_abi_dumps_from_shared_libraries(git_worktree_path, version)
        self._cleanup_worktree(git_worktree_path)

    def _remove_children_with_tag(self, parent, tag):
        children = parent.getchildren()
        for child in children:
            if child.tag == tag:
                parent.remove(child)
            else:
                self._remove_children_with_tag(child, tag)

    def _remove_extra_detail_from_report(self, report_root):
        for tag in ['test_info', 'test_results', 'problem_summary',
                'added_symbols', 'removed_symbols', 'affected']:
            self._remove_children_with_tag(report_root, tag)

        for report in report_root:
            for problems in report.getchildren()[:]:
                if not problems.getchildren():
                    report.remove(problems)

    def get_abi_compatibility_report(self):
        compatibility_report = ""
        compliance_return_code = 0
        shared_modules = list(set(self.old_version.modules.keys()) &
                              set(self.new_version.modules.keys()))
        for mbed_module in shared_modules:
            output_path = os.path.join(
                self.report_dir, "{}-{}-{}.html".format(
                    mbed_module, self.old_version.revision,
                    self.new_version.revision
                )
            )
            abi_compliance_command = [
                "abi-compliance-checker",
                "-l", mbed_module,
                "-old", self.old_version.abi_dumps[mbed_module],
                "-new", self.new_version.abi_dumps[mbed_module],
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
                    self._remove_extra_detail_from_report(report_root)
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
            os.remove(self.old_version.abi_dumps[mbed_module])
            os.remove(self.new_version.abi_dumps[mbed_module])
        if self.can_remove_report_dir:
            os.rmdir(self.report_dir)
        self.log.info(compatibility_report)
        return compliance_return_code

    def check_for_abi_changes(self):
        self._check_repo_path()
        self._check_abi_tools_are_installed()
        self._get_abi_dump_for_ref(self.old_version)
        self._get_abi_dump_for_ref(self.new_version)
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
            "-v", "--verbose", action="store_true",
            help="set verbosity level",
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
        old_version = RepoVersion("old", abi_args.old_repo, abi_args.old_rev,
                 abi_args.old_crypto_repo, abi_args.old_crypto_rev)
        new_version = RepoVersion("new", abi_args.new_repo, abi_args.new_rev,
                 abi_args.new_crypto_repo, abi_args.new_crypto_rev)
        abi_check = AbiChecker(
            abi_args.verbose, old_version, new_version, abi_args.report_dir,
            abi_args.keep_all_reports, abi_args.brief, abi_args.skip_file
        )
        return_code = abi_check.check_for_abi_changes()
        sys.exit(return_code)
    except Exception:
        traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    run_main()
