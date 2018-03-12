#!/usr/bin/env python3
#
# This file is part of mbed TLS (https://tls.mbed.org)
#
# Copyright (c) 2018, Arm Limited, All Rights Reserved
#
# Purpose
#
# This script requires python 3. This script checks for undesired changes
# to the source code, including file permission changes, presence of tabs,
# non-Unix line endings, trailing whitespace, and TODO comments.
# Note: must be run from Mbed TLS root.

import os
import argparse
import logging


class IntegrityChecker(object):

    def __init__(self, log_file):
        self.check_repo_path()
        self.logger = None
        self.setup_logger(log_file)
        self.source_files_to_check = (
            ".c", ".h", ".function", ".data",
            ".md", "Makefile", "CMakeLists.txt"
        )
        self.script_file_types = (".sh", ".pl", ".py")
        self.permission_issues = []
        self.end_of_file_newline_issues = []
        self.line_ending_issues = {
            "report_heading": "Non Unix line endings:", "files": {}
        }
        self.trailing_whitespace = {
            "report_heading": "Trailing whitespace:", "files": {}
        }
        self.tab_issues = {
            "report_heading": "Tabs present:", "files": {}
        }
        self.todo_issues = {
            "report_heading": "TODO present:", "files": {}
        }

    def check_repo_path(self):
        if not __file__ == os.path.join(".", "scripts", "integrity_check.py"):
            raise Exception("Must be run from Mbed TLS root")

    def setup_logger(self, log_file, level=logging.INFO):
        self.logger = logging.getLogger()
        self.logger.setLevel(level)
        if log_file:
            handler = logging.FileHandler(log_file)
            self.logger.addHandler(handler)
        else:
            console = logging.StreamHandler()
            self.logger.addHandler(console)

    def check_file_permissions(self, filepath):
        if not (os.access(filepath, os.X_OK) ==
                filepath.endswith(self.script_file_types)):
            self.permission_issues.append(filepath)

    def check_file_content(self, filepath):
        with open(filepath, "r") as f:
            self.line_ending_issues["files"][filepath] = []
            self.trailing_whitespace["files"][filepath] = []
            self.tab_issues["files"][filepath] = []
            self.todo_issues["files"][filepath] = []
            allow_trailing_whitespace = filepath.endswith(".md")
            allow_tabs = filepath.endswith("Makefile")
            line = None
            for i, line in enumerate(iter(f.readline, "")):
                if "\r" in line:
                    self.line_ending_issues["files"][filepath].append(i + 1)
                if (not allow_trailing_whitespace and
                        line.rstrip("\r\n") != line.rstrip()):
                    self.trailing_whitespace["files"][filepath].append(i + 1)
                if not allow_tabs and "\t" in line:
                    self.tab_issues["files"][filepath].append(i + 1)
                if "TODO" in line:
                    self.todo_issues["files"][filepath].append(i + 1)
            if line is not None and not line.endswith("\n"):
                self.end_of_file_newline_issues.append(filepath)

    def check_files(self):
        for root, dirs, files in sorted(os.walk(".")):
            for filepath in sorted(files):
                absolute_filepath = os.path.join(root, filepath)
                if os.path.join("yotta", "module") in absolute_filepath:
                    continue
                self.check_file_permissions(absolute_filepath)
                if not filepath.endswith(self.source_files_to_check):
                    continue
                self.check_file_content(absolute_filepath)

    def output_issues(self):
        if self.permission_issues:
            self.logger.info("Incorrect file permissions:")
            for issue in self.permission_issues:
                self.logger.info(issue)
            self.logger.info("")
        if self.end_of_file_newline_issues:
            self.logger.info("Missing newline at end of file:")
            for issue in self.end_of_file_newline_issues:
                self.logger.info(issue)
            self.logger.info("")
        for category in [self.line_ending_issues,
                         self.trailing_whitespace,
                         self.tab_issues,
                         self.todo_issues]:
            if any(category["files"].values()):
                self.logger.info(category["report_heading"])
                for filename, lines in sorted(category["files"].items()):
                    if lines:
                        self.logger.info("{}: {}".format(
                            filename, ", ".join(str(x) for x in lines)
                        ))
                self.logger.info("")


def run_main():
    parser = argparse.ArgumentParser(
        description=(
            "This script checks for undesired changes to the source code, "
            "including file permission changes, presence of tabs, "
            "non-Unix line endings, trailing whitespace, and TODO comments. "
            "Note: must be run from Mbed TLS root."
        )
    )
    parser.add_argument(
        "-l", "--log_file", type=str, help="path to optional output log",
    )
    check_args = parser.parse_args()
    integrity_check = IntegrityChecker(check_args.log_file)
    integrity_check.check_files()
    integrity_check.output_issues()


if __name__ == "__main__":
    run_main()
