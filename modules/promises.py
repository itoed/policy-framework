from __future__ import print_function
from StringIO import StringIO

class Promise(object):
    def __init__(self):
        self._promise_kept = False
        self._promise_repaired = False
        self._repair_failed = False
        self._errlog = None
        self._keptlog = None
        self._repairlog = None

    @property
    def promise_kept(self):
        return self._promise_kept

    @property
    def promise_repaired(self):
        return self._promise_repaired

    @property
    def promise_ok(self):
        return self._promise_kept or self._promise_repaired

    @property
    def repair_failed(self):
        return self._repair_failed

    def err(self, message):
        self._promise_kept = False
        self._promise_repaired = False
        self._repair_failed = True
        if not self._errlog:
            self._errlog = StringIO()
        self._errlog.seek(0, 2)
        print(message, file=self._errlog)

    def keptmsg(self, message):
        if not self._keptlog:
            self._keptlog = StringIO()
        self._keptlog.seek(0, 2)
        print(message, file=self._keptlog)

    def repairmsg(self, message):
        if not self._repairlog:
            self._repairlog = StringIO()
        self._repairlog.seek(0, 2)
        print(message, file=self._repairlog)

    @property
    def errlog(self):
        if self._errlog:
            self._errlog.seek(0)
        return self._errlog

    @property
    def keptlog(self):
        if self._keptlog:
            self._keptlog.seek(0)
        return self._keptlog

    @property
    def repairlog(self):
        if self._repairlog:
            self._repairlog.seek(0)
        return self._repairlog

class CompoundPromise(Promise):
    def __init__(self):
        super(CompoundPromise, self).__init__()
        self.promises = []

    def add_promise(self, promise):
        self.promises.append(promise)

    @property
    def promise_kept(self):
        return (not self._repair_failed and
            all([ p.promise_kept for p in self.promises ]))

    @property
    def promise_repaired(self):
        return (not self._repair_failed and
            all([ p.promise_ok for p in self.promises ]) and
            any([ p.promise_repaired for p in self.promises ]))

    @property
    def promise_ok(self):
        return self.promise_kept or self.promise_repaired

    @property
    def repair_failed(self):
        return (self._repair_failed or
            any([ p.repair_failed for p in self.promises ]))
