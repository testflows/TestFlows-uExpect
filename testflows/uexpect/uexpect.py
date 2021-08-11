# Copyright 2019 Katteli Inc.
# TestFlows.com Open-Source Software Testing Framework (http://testflows.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import sys
import pty
import time
import termios
import re
import codecs

from threading import Thread, Event, Lock
from subprocess import Popen
from queue import Queue, Empty

__all__ = ["spawn", "ExpectTimeoutError"]

class TimeoutError(Exception):
    def __init__(self, timeout):
        self.timeout = timeout

    def __str__(self):
        return 'Timeout %.3fs' % float(self.timeout)

class ExpectTimeoutError(Exception):
    def __init__(self, pattern, timeout, buffer):
        self.pattern = pattern
        self.timeout = timeout
        self.buffer = buffer

    def __str__(self):
        s = 'Timeout %.3fs ' % float(self.timeout)
        if self.pattern:
            s += 'for %s ' % repr(self.pattern.pattern)
        if self.buffer:
            s += 'buffer %s ' % repr(self.buffer[:])
            s += 'or \'%s\'' % ','.join(['%x' % ord(c) for c in self.buffer[:]])
        return s

class IO(object):
    class EOF(object):
        pass

    class Timeout(object):
        pass

    EOF = EOF
    TIMEOUT = Timeout

    class Logger(object):
        def __init__(self, logger, prefix=''):
            self._logger = logger
            self._prefix = prefix
            self.write(self._prefix)

        def write(self, data):
            if not data:
                return
            self._logger.write(data.replace('\n','\n' + self._prefix))

        def flush(self):
            self._logger.flush()

    def __init__(self, process, master, queue, reader):
        self.process = process
        self.master = master
        self.queue = queue
        self.buffer = None
        self.before = None
        self.after = None
        self.match = None
        self.pattern = None
        self.reader = reader
        self._timeout = None
        self._logger = None
        self._logger_buffer_pos = 0
        self._eol = ''
        self._closed = False
        self._lock = Lock()

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    def logger(self, logger=None, prefix=''):
        if logger:
            self._logger = self.Logger(logger, prefix=prefix)
        return self._logger

    def timeout(self, timeout=None):
        if timeout:
            self._timeout = timeout
        return self._timeout

    def eol(self, eol=None):
        if eol:
            self._eol = eol
        return self._eol

    def close(self, force=True):
        with self._lock:
            if not self._closed:
                try:
                    self.reader['kill_event'].set()
                    os.system('pkill -TERM -P %d' % self.process.pid)
                    if force:
                        self.process.kill()
                    else:
                        self.process.terminate()
                    os.close(self.master)
                    if self._logger:
                        self._logger.write('\n')
                        self._logger.flush()
                finally:
                    self._closed = True
        
    def send(self, data, eol=None, delay=None):
       if eol is None:
           eol = self._eol
       if delay is not None:
           time.sleep(delay)
       return self.write(data + eol)

    def write(self, data):
        with self._lock:
            if self._closed:
                raise IOError("closed")
            n = os.write(self.master, data.encode("utf-8"))
            termios.tcdrain(self.master)
            return n

    def expect(self, pattern, timeout=None, escape=False, expect_timeout=False):
        self.match = None
        self.before = None
        self.after = None
        if escape:
            pattern = re.escape(pattern)
        pattern = re.compile(pattern)
        if timeout is None:
            timeout = self._timeout
        timeleft = timeout
        if timeleft is None:
            timeleft = sys.maxsize
        while True:
            start_time = time.time()

            if self.buffer is not None:
                self.match = pattern.search(self.buffer, 0)
                if self.match is not None:
                    if self._logger:
                        self._logger.write(self.buffer[self._logger_buffer_pos:self.match.end()])
                    self.after = self.buffer[self.match.start():self.match.end()]
                    self.before = self.buffer[:self.match.start()]
                    self.buffer = self.buffer[self.match.end():]
                    self._logger_buffer_pos = 0
                    break
                elif self._logger and not expect_timeout:
                    self._logger.write(self.buffer[self._logger_buffer_pos:])
                    self._logger_buffer_pos = len(self.buffer)

            try:
                data = None
                data = self.read(timeout=min(timeleft, 0.1), raise_exception=True)
            except TimeoutError:
                elapsed = time.time() - start_time
                timeleft = max(timeleft - elapsed, 0)
                if timeleft <= 0:
                    if self._logger and not expect_timeout:
                        self._logger.write((self.buffer or '')[self._logger_buffer_pos:] + '\n')
                        self._logger.flush()
                    exception = ExpectTimeoutError(pattern, timeout, self.buffer)
                    self.before = self.buffer
                    self.after = None
                    if not expect_timeout:
                        self.buffer = None
                        self._logger_buffer_pos = 0
                    if expect_timeout:
                        return
                    raise exception
            else:
                elapsed = time.time() - start_time
                timeleft = max(timeleft - elapsed, 0)
            if data:
                self.buffer = (self.buffer + data) if self.buffer else data

        return self.match

    def read(self, timeout=0, raise_exception=False):
        with self._lock:
            if self._closed:
                raise IOError("closed")
            data = ''
            timeleft = timeout
            try:
                while timeleft >= 0 :
                    start_time = time.time()
                    d = self.queue.get(timeout=timeleft)
                    if isinstance(d, BaseException):
                        raise d
                    data += d
                    if data:
                        break
                    elapsed = time.time() - start_time
                    timeleft = max(timeleft - elapsed, 0)
            except Empty:
                if data:
                    return data
                if raise_exception:
                    raise TimeoutError(timeout)
                pass
            if not data and raise_exception:
                raise TimeoutError(timeout)

            return data

def _reader(out, queue, kill_event, encoding="utf-8", errors="backslashreplace"):
    """Reader thread.

    :param out: pty master to read from
    :param queue: data queue
    :param kill_event: kill event
    """
    decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
    data = bytes()
    while True:
        try:
            data += os.read(out, 65536)
            queue.put(decoder.decode(data))
            data = bytes()
        except OSError as e:
            queue.put(e)
            if e.errno in (5, 9):
                return
            raise
        except BaseException as e:
            queue.put(e)
            if kill_event.is_set():
                return
            raise

def spawn(command):
    master, slave = pty.openpty()
    process = Popen(command, preexec_fn=os.setsid, stdout=slave, close_fds=False, stdin=slave, stderr=slave, bufsize=1)
    os.close(slave)

    queue = Queue()
    reader_kill_event = Event()
    thread = Thread(target=_reader, args=(master, queue, reader_kill_event))
    thread.daemon = True
    thread.start()

    return IO(process, master, queue, reader={'thread':thread, 'kill_event':reader_kill_event})
