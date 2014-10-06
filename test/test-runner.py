#!/usr/bin/python

from fnmatch import fnmatch
from Queue import Queue, Empty as QueueEmpty
from shutil import rmtree
from tempfile import mkdtemp
from textwrap import wrap

import argparse
import errno
import fcntl
import json
import os
import re
import select
import shlex
import stat
import subprocess
import sys
import threading

def make_async(fd):
    """ Add the O_NONBLOCK flag to a file descriptor """
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)

def read_async(fd):
    """ Read a line from a file descriptor, ignoring EAGAIN errors """
    try:
        return fd.readline()
    except IOError, e:
        if e.errno != errno.EAGAIN:
            raise e
        else:
            return ''

class Message(object):
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value

class CFEngineMessage(Message):
    def __init__(self, value):
        super(CFEngineMessage, self).__init__(value)

class CFEngineError(CFEngineMessage):
    def __init__(self, value):
        super(CFEngineError, self).__init__(value)

class CFEngineReport(CFEngineMessage):
    PREFIX = "R: "
    def __init__(self, prefixed_value):
        super(CFEngineReport, self).__init__(prefixed_value)
        if not prefixed_value.startswith(self.PREFIX):
            raise Exception("Value does not start with '" + self.PREFIX +
                "': " + prefixed_value)
        self.contents =  prefixed_value[len(self.PREFIX):]

class TestFileStart(Message):
    def __init__(self, name):
        self.name = name
        self.value = "Started " + name

class TestFileResult(Message):
    def __init__(self, name, bundle_count, passed_count):
        self.name = name
        self.value = "{0} of {1} tests passed".format(passed_count, bundle_count)

class TestSuiteResult(Message):
    def __init__(self, bundle_count, passed_count):
        self.value = "Total: {0} of {1} tests passed".format(passed_count, bundle_count)

class TestAssertionError(Message):
    def __init__(self, value):
        self.value = value

class TestRunnerError(Message):
    def __init__(self, value):
        self.value = value

class TestResult(Message):
    def __init__(self, bundle, sequence_num, passed):
        self.bundle = bundle
        self.sequence_num = sequence_num
        self.passed = passed
        self.label = "Pass" if passed else "FAIL"
        self.value = self.label + " " + bundle

class CFAgentProcess(object):
    CMD_TEMPLATE = "cf-agent -{0}Kf {1} -b {2}"

    def __init__(self, filename, bundlesequence=[], opts=""):
        cmdstr = self.CMD_TEMPLATE.format(opts, filename, ",".join(bundlesequence))
        self.command = shlex.split(cmdstr)

    def execute(self):
        self.process = subprocess.Popen(self.command, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE);
        self.stdout = self.process.stdout
        self.stderr = self.process.stderr
        make_async(self.stdout)
        make_async(self.stderr)

    def readmsg(self):
        returnCode = None
        while True:
            # Wait for data to become available
            select.select([self.stdout, self.stderr], [], [])
            # Try reading some data from each
            line_out = read_async(self.stdout).rstrip()
            if line_out:
                if line_out.startswith(CFEngineReport.PREFIX):
                    yield CFEngineReport(line_out)
                else:
                    yield CFEngineMessage(line_out)
            line_err = read_async(self.stderr).rstrip()
            if line_err:
                yield CFEngineError(line_err)
            returnCode = self.process.poll()
            if returnCode != None:
                break

class TestAssertion(object):
    PASS = "pass"
    FAIL = "fail"
    RESULT_KEY = "result"
    ERROR_KEY = "error"
    def __init__(self, assert_dict):
        self.valid = True
        self.passed = False
        self.errormsg = ""
        #
        #   TO-DO VALIDATION:
        #
        #   MISSING RESULT KEY
        #   MISSING ERROR KEY ON FAILURES
        #   UNKNOWN RESULT VALUE
        #
        if self.RESULT_KEY not in assert_dict:
            self.valid = False
        else:
            result = assert_dict[self.RESULT_KEY] 
            if result == self.PASS:
                self.passed = True
            elif result == self.FAIL:
                if self.ERROR_KEY not in assert_dict:
                    self.valid = False
                else:
                    self.errormsg = assert_dict[self.ERROR_KEY]
            else:
                self.valid = False

class TestBundle(object):
    """
        Holds information from a test bundle execution
    """
    ASSERTION_REPORT_PREFIX = "test_report="
    TEMP_DIR_TAGNAME = "tmpdir"

    def __init__(self, name, sequence_num, tags, process, message_queue):
        self.name = name
        self.process = process
        self.message_queue = message_queue
        self.valid = True
        self.passed = False
        self.messages = []
        self.assertions = []
        self.sequence_num = sequence_num
        self.has_tmpdir = self.TEMP_DIR_TAGNAME in tags

    def execute(self):
        if self.has_tmpdir:
            tmpdir = mkdtemp()
            os.environ["TMP_DIR"] = tmpdir
        try:
            self.process.execute()
            for msg in self.process.readmsg():
                if type(msg) == CFEngineReport and msg.contents.startswith(self.ASSERTION_REPORT_PREFIX):
                    #   TO-DO VALIDATION:
                    #   NO ASSERTION REPORT
                    #   MULTIPLE ASSERTION REPORTS
                    test_report = json.loads(msg.contents[len(self.ASSERTION_REPORT_PREFIX):])
                    for assert_id in sorted(test_report):
                        assertion = TestAssertion(test_report[assert_id])
                        self.assertions.append(assertion)
                        if not assertion.passed:
                            self.messages.append(TestAssertionError(assertion.errormsg))
                else:
                    self.messages.append(msg)
            if not self.assertions:
                self.messages.append(TestRunnerError("No assertions"))
            else:
                self.passed = all([ assertion.passed for assertion in self.assertions])
        finally:
            if self.has_tmpdir:
                rmtree(tmpdir)
        message_queue.put(TestResult(self.name, self.sequence_num, self.passed))
        for msg in self.messages:
            message_queue.put(msg)

class TestFile(object):
    """
        A Test file...
    """
    BUNDLE_NAME_KEY = "name"
    BUNDLE_TAGS_KEY = "tags"

    def __init__(self, main_test_script, path, message_queue, stop_event,
            bundle_expr=".*", opts=""):
        self.valid = True
        self.path = path
        self.main_test_script = main_test_script
        self.message_queue = message_queue
        self.stop_event = stop_event
        self.basename = os.path.basename(path)
        self.name = os.path.splitext(self.basename)[0]
        self.bundle_expr = bundle_expr
        self.testbundles = []
        self.passed = False
        self.bundle_count = 0
        self.passed_count = 0
        self.opts = opts

    def _find_test_bundles(self):
        """
            Invokes the find_test_bundle agent bundle in the test.cf file
            through cf-agent and parses each report line in the format
            '{
                "name":     "namespace:bundlename",
                "tags":     [ "test", "tag1", "tag2" ]
            }'.
            and populates a list of testbundles in sorted order
        """
        os.environ["TEST_PATH"] = self.path
        finder_process = CFAgentProcess(self.main_test_script, [ "test:find_test_bundles" ])
        finder_process.execute()
        unordered_dicts = []
        for msg in finder_process.readmsg():
            if type(msg) is not CFEngineReport:
                self.valid = False
                self.message_queue.put(TestRunnerError(self.basename + " - " + msg.value))
                self.message_queue.put(TestRunnerError("CFEngineReport expected"))
            else:
                bundle_dict = json.loads(msg.contents)
                if self.BUNDLE_NAME_KEY not in bundle_dict:
                    self.valid = False
                    self.message_queue.put(TestRunnerError(
                        "Missing key '{0}'".format(self.BUNDLE_NAME_KEY)))
                if self.BUNDLE_TAGS_KEY not in bundle_dict:
                    self.valid = False
                    self.message_queue.put(TestRunnerError(
                        "Missing key '{0}'".format(self.BUNDLE_TAGS_KEY)))
                if self.valid:
                    unordered_dicts.append(bundle_dict)

        ordered_dicts = sorted(unordered_dicts,
            key=lambda bundle_dict: bundle_dict[self.BUNDLE_NAME_KEY])

        if self.valid:
            for i, bundle_dict in enumerate(ordered_dicts, start=1):
                name = bundle_dict[self.BUNDLE_NAME_KEY]
                tags = bundle_dict[self.BUNDLE_TAGS_KEY]
                process = CFAgentProcess(self.main_test_script, [ name, "test:report_results" ], opts=self.opts)
                if re.match(self.bundle_expr, name):
                    self.testbundles.append(TestBundle(name, i, tags, process,
                        self.message_queue))

    def execute(self):
        self.message_queue.put(TestFileStart(self.name))
        self._find_test_bundles()
        for bundle in self.testbundles:
            if self.stop_event.is_set():
                break
            bundle.execute()
        self.bundle_count = len(self.testbundles)
        self.passed_count = len([ b for b in self.testbundles if b.passed ])
        self.passed = self.passed_count == self.bundle_count
        self.message_queue.put(TestFileResult(self.name, self.bundle_count,
            self.passed_count))

class TestSuite(threading.Thread):
    PATH_EXPR = "*"
    def __init__(self, main_test_script, tests_directory, queue, path_expr=PATH_EXPR,
            bundle_expr=".*", opts=""):
        super(TestSuite, self).__init__()

        self.queue = queue
        self.done = threading.Event()

        test_paths = [
            os.path.join(dirpath, f)
            for dirpath, dirnames, files in os.walk(tests_directory)
            for f in files if fnmatch(os.path.join(dirpath, f), path_expr) and f.endswith(".cf")
        ]

        self.test_files = [ TestFile(main_test_script, path, message_queue, self.done,
            bundle_expr=bundle_expr, opts=opts) for path in sorted(test_paths) ]

        self.total_count = 0
        self.passed_count = 0

    def run(self):
        try:
            for t in self.test_files:
                if self.done.is_set():
                    break
                t.execute()
            self.total_count = sum([ t.bundle_count for t in self.test_files ])
            self.passed_count = sum([ t.passed_count for t in self.test_files ])
            message_queue.put(TestSuiteResult(self.total_count, self.passed_count))
        finally:
            self.done.set()

    def stop(self):
        self.done.set()

    def is_done(self):
        return self.done.is_set()

class Color:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    TEXTWHITE = '\033[37m'
    DARKGRAY = '\033[90m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'

    @classmethod
    def okgreen(cls, text):
        return cls.OKGREEN + text + cls.ENDC

    @classmethod
    def okblue(cls, text):
        return cls.OKBLUE + text + cls.ENDC

    @classmethod
    def error(cls, text):
        return cls.FAIL + text + cls.ENDC

    @classmethod
    def warning(cls, text):
        return cls.WARNING + text + cls.ENDC

    @classmethod
    def fail(cls, text):
        return cls.FAIL + text + cls.ENDC

    @classmethod
    def textwhite(cls, text):
        return cls.TEXTWHITE + text + cls.ENDC

    @classmethod
    def darkgray(cls, text):
        return cls.DARKGRAY + text + cls.ENDC

#
#   Parse arguments
#
parser = argparse.ArgumentParser()
parser.add_argument("-v", "--verbose", help="Increase output verbosity",
                    action="store_true")
parser.add_argument("-e", "--extraverbose", help="Increase output verbosity with errors",
                    action="store_true")
parser.add_argument("-b", "--bundlesmatching", help="Run only bundles matching expression",
                    default=".*")
parser.add_argument("-o", "--options", help="Options passed to cf-agent", default="")
parser.add_argument("test", help="Name of test file without extension", nargs="?",
                    default=TestSuite.PATH_EXPR)
args = parser.parse_args()

#
#   Run suite
#
if __name__ == '__main__':
    script_path = os.path.realpath(__file__)
    script_dir = os.path.dirname(script_path)
    main_test_script = os.path.join(script_dir, "test.cf")

    message_queue = Queue()

    workdir =  os.path.abspath(os.path.join(script_dir, os.pardir))
    os.environ["TEST_WORKDIR"] = workdir

    cf_files = [
        os.path.join(dirpath, f)
        for dirpath, dirnames, filenames in os.walk(workdir)
        for f in filenames if f.endswith(".cf")
    ]
    for f in cf_files:
        os.chmod(f, stat.S_IRUSR|stat.S_IWUSR)

    unit_test_dir = os.path.join(script_dir, "tests");

    test_suite = TestSuite(main_test_script, unit_test_dir, message_queue,
        path_expr=args.test, opts=args.options,
        bundle_expr=args.bundlesmatching)
    test_suite.start()

    try:
        while not test_suite.is_done() or not message_queue.empty():
            try:
                msg = message_queue.get(timeout=0.05)
                if type(msg) == TestFileStart:
                    print Color.okblue(msg.value)
                    print Color.okblue("=" * 104)
                elif type(msg) == TestFileResult:
                    print Color.okblue("=" * 104)
                    print Color.okblue(msg.value)
                    print
                elif type(msg) == TestResult:
                    if args.verbose and msg.sequence_num > 1:
                        print
                    label = Color.okgreen(msg.label) if msg.passed else Color.fail(msg.label)
                    print "{0} {1}".format(label, Color.textwhite(msg.bundle))
                elif type(msg) == TestAssertionError:
                    print "    ", Color.warning(msg.value)
                elif type(msg) == TestRunnerError:
                    print "    ", Color.error("Test Runner Error: " + msg.value)
                elif type(msg) == TestSuiteResult:
                    if len(test_suite.test_files) > 1:
                        print Color.okblue(msg.value)
                elif type(msg) == CFEngineError:
                    if args.extraverbose:
                        for line in wrap(msg.value, width=100):
                            print "    ", Color.darkgray(line)
                elif isinstance(msg, CFEngineMessage):
                    if args.verbose:
                        for line in wrap(msg.value, width=100):
                            print "    ", Color.darkgray(line)
                else:
                    print msg
            except QueueEmpty:
                continue
    finally:
        test_suite.stop()
        test_suite.join()
