#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
Nightmare Fuzzing Project generic test case minimizer
@author: joxean

@description: This generic test case minimizer, as of 9 of May of 2014, 
only works with some specific mutators' test cases as it doesn't perform
any diffing by itself but, rather, reads the .diff files generated by
the mutator and replaces mutated data byte-per-byte. As of end of 2015,
it also supports generic text based documents and it works by simply 
removing lines. As simple as it sounds.

The generic algorithm using .diff files is as simple as this:

 1) Read the positions from the .diff file where changes were made.
 2) Read the modified bytes at those positions.
 3) Read the whole template file.
 4) Apply only one (byte) change at a time and check for a crash.
 5) If applying only one byte change doesn't work, consider that the 1st
   change applied to the file is mandatory. Apply it to the internally
   read template buffer and remove from the list of changes to check.
 6) Go back to 4.

In the future, the algorithm may be changed adding the following steps:

 * If a crashing change was found after applying a number of other small
   changes before try minimizing it even more by undoing the previously
   applied small patches.
   
Or the following "new" algorithm:

 * Start with all the changes applied. Remove one-per-one all of them
   until the process doesn't crash any more.

This generic test case minimizer was written for Linux/Unix platforms,
I don't think it works as is in Windows as it simply uses the return
codes instead of a debugging interface (because it's easier and less
problematic over all).

The generic line based minimizer added at the end of 2015 works like in
the following way:

 1) Starting from the 1st line, remove a random number of lines between
    1 to a maximum of 10% the total file.
 2) If an exception (any) is still happening, remove these lines and 
    continue.
 3) If the change causes the target not to crash any more, restore the
    dropped lines, move to the next one and go back to 1).
 4) After the last line is reached, it then iterates again from line 0,
    but this time it removes a single line each time.

Usually, the file is mostly minimized at the first round, however, it
can still be minimized even more with the 2nd pass. For some file types,
like JavaScript inside HTML files, removing line-per-line in the 2nd
pass doesn't make sense as it will remove, for example, the lines with
the code "function xxx()" and likely the "{" character. One can specify
the number of lines to remove in the 2nd step by setting the directive
"lines-to-rip" in the configuration file. It's also possible to change
the maximum percent of lines to remove during the first pass by setting
the value of "lines-percent". It's also possible to tell that we want to
force it to perform exclusively a line-per-line minimization, instead of
using the heuristic of removing a percent of lines in the 1st pass. Is
not recommended because, overall, it's better to use this heuristic but,
perhaps, it could be useful for somebody tomorrow.

"""

import os
import sys
import shutil
import random
import tempfile
import ConfigParser

from hashlib import sha1

script_path = os.path.dirname(os.path.realpath(__file__))
sys.path.append(script_path)
tmp_path = os.path.join(script_path, "..")
sys.path.append(tmp_path)
tmp_path = os.path.join(tmp_path, "runtime")
sys.path.append(tmp_path)
tmp_path = os.path.join(tmp_path, "../lib/")
sys.path.append(tmp_path)
tmp_path = os.path.join(tmp_path, "../lib/interfaces")
sys.path.append(tmp_path)

from nfp_log import log
from nfp_process import TimeoutCommand, RETURN_SIGNALS

try:
  from lib.interfaces import vtrace_iface, gdb_iface, pykd_iface
  has_pykd = True
except ImportError:
  has_pykd = False
  from lib.interfaces import vtrace_iface, gdb_iface

#-----------------------------------------------------------------------
class CGenericMinimizer:
  def __init__(self, cfg, section):
    self.cfg = cfg
    self.section = section
    self.read_configuration()

    self.diff = []
    self.template = []
    self.crash = {}

  def read_diff(self, diff):
    with open(diff, "rb") as f:
      for line in f.readlines():
        # Ignore lines with comments
        if line.startswith("#"):
          continue
        line = line.strip("\n").strip("\r")
        if line.isdigit():
          self.diff.append(int(line))

  def read_template(self, template):
    self.template = bytearray(open(template, "rb").read())
  
  def read_crash(self, crash):
    tmp = bytearray(open(crash, "rb").read())
    self.crash = {}
    
    for pos in self.diff:
      self.crash[pos] = tmp[pos]

  def read_configuration(self):
    if not os.path.exists(self.cfg):
      raise Exception("Invalid configuration file given")

    parser = ConfigParser.SafeConfigParser()
    parser.optionxform = str
    parser.read(self.cfg)
    self.parser = parser

    if self.section not in parser.sections():
      raise Exception("Section %s does not exists in the given configuration file" % self.section)

    try:
      self.pre_command = parser.get(self.section, 'pre-command')
    except:
      # Ignore it, it isn't mandatory
      self.pre_command = None

    try:
      self.pre_iterations = int(parser.get(self.section, 'pre-iterations'))
    except:
      # Ignore it, it isn't mandatory
      self.pre_iterations = 1

    try:
      self.post_command = parser.get(self.section, 'post-command')
    except:
      # Ignore it, it isn't mandatory
      self.post_command = None

    try:
      self.post_iterations = int(parser.get(self.section, 'post-iterations'))
    except:
      # Ignore it, it isn't mandatory
      self.post_iterations = 1

    try:
      self.command = parser.get(self.section, 'command')
    except:
      raise Exception("No command specified in the configuration file for section %s" % self.section)
    
    try:
      self.extension = parser.get(self.section, 'extension')
    except:
      raise Exception("No extension specified in the configuration file for section %s" % self.section)

    try:
      self.timeout = parser.get(self.section, 'minimize-timeout')
    except:
      # Default timeout is 90 seconds
      self.timeout = 90
    
    if self.timeout.lower() != "auto":
      self.timeout = int(self.timeout)

    try:
      environment = parser.get(self.section, 'environment')
      self.env = dict(parser.items(environment))
    except:
      self.env = {}
    
    try:
      self.cleanup = parser.get(self.section, 'cleanup-command')
    except:
      self.cleanup = None
    
    try:
      self.signal = int(parser.get(self.section, 'signal'))
    except:
      self.signal = None
    
    try:
      self.mode = parser.get(self.section, 'mode')
      if self.mode.isdigit():
        self.mode = int(self.mode)
    except:
      self.mode = 32

    try:
      self.windbg_path = parser.get(self.section, 'windbg-path')
    except:
      self.windbg_path = None

    try:
      self.exploitable_path = parser.get(self.section, 'exploitable-path')
    except:
      self.exploitable_path = None

    # Left "for now", for backward compatibility reasons.
    # Subject to be removed at any time. See below why.
    try:
      if parser.getboolean(self.section, 'use-gdb'):
        self.iface = gdb_iface
      else:
        self.iface = vtrace_iface
    except:
      self.iface = vtrace_iface

    try:
      self.debugging_interface = parser.get(self.section, 'debugging-interface')
      if self.debugging_interface == "pykd":
        self.iface = pykd_iface
      elif self.debugging_interface == "gdb":
        self.iface = gdb_iface
      else:
        self.iface = vtrace_iface
    except:
      self.debugging_interface = None
      self.iface = vtrace_iface

  def minimize(self, template, crash, diff, outdir):
    self.read_diff(diff)
    self.read_template(template)
    self.read_crash(crash)

    log("Performing test case minimization with a total of %d change(s)" % len(self.diff))
    start_at = os.getenv("NIGHTMARE_ITERATION")
    if start_at is not None:
      start_at = int(start_at)
      log("Starting from iteration %d\n" % start_at)
    else:
      start_at = 0

    self.do_try(outdir, start_at)
  
  def execute_command(self, cmd, timeout):
    ret = None
    if self.debugging_interface is None:
      cmd_obj = TimeoutCommand(cmd)
      ret = cmd_obj.run(timeout=self.timeout)
      if cmd_obj.stderr is not None:
        print cmd_obj.stderr
    else:
      self.iface.timeout = self.timeout
      if not has_pykd or self.iface != pykd_iface:
        crash = self.iface.main(cmd)
      else:
        os.putenv("_NT_SYMBOL_PATH", "")
        crash = pykd_iface.main([cmd], timeout, mode=self.mode, windbg_path=self.windbg_path, exploitable_path=self.exploitable_path)

      if crash is not None:
        ret = 0xC0000005 # Access violation in Windows

    return ret

  def do_try(self, outdir, start_at=0):
    # Try to minimize to just one change
    current_change = 0
    minimized = False
    iteration = 0
    for i in range(len(self.diff)):
      for pos in self.diff:
        if start_at <= iteration:
          log("Minimizing, iteration %d (Max. %d)..." % (iteration, (len(self.diff)) * len(self.diff)))
          temp_file = tempfile.mktemp()
          buf = bytearray(self.template)
          if pos not in self.crash:
            continue
          
          buf[pos] = self.crash[pos]

          with open(temp_file, "wb") as f:
            f.write(buf)

          try:
            for key in self.env:
              os.putenv(key, self.env[key])

            if self.pre_command is not None:
              log("Running pre-command %s" % self.pre_command)
              os.system(self.pre_command)

            cmd = "%s %s" % (self.command, temp_file)
            ret = self.execute_command(cmd, self.timeout)

            if self.post_command is not None:
              log("Running post-command %s" % self.post_command)
              os.system(self.post_command)

            if ret in RETURN_SIGNALS or (self.signal is not None and ret == self.signal) or \
             self.crash_file_exists():
              log("Successfully minimized, caught signal %d (%s)!" % (ret, RETURN_SIGNALS[ret]))
              filename = sha1(buf).hexdigest()
              filename = os.path.join(outdir, "%s%s" % (filename, self.extension))
              shutil.copy(temp_file, filename)
              log("Minized test case %s written to disk." % filename)
              minimized = True
              break
          finally:
            os.remove(temp_file)

        if minimized:
          break

        iteration += 1

      if minimized:
          break

      value = self.diff.pop()
      if value in self.crash:
        self.template[value] = self.crash[value]
        del self.crash[value]

    if not minimized:
      log("Sorry, could not minimize crashing file!")

#-----------------------------------------------------------------------
class CLineMinimizer(CGenericMinimizer):
  def __init__(self, cfg, section):
    CGenericMinimizer.__init__(self, cfg, section)
    self.strip_empty_lines = True

    self.read_configuration()
  
  def read_configuration(self):
    CGenericMinimizer.read_configuration(self)
    try:
      self.line_per_line = bool(self.parser.get(self.section, 'line-per-line'))
    except:
      self.line_per_line = False
    
    try:
      self.lines_to_rip = int(self.parser.get(self.section, 'lines-to-rip'))
    except:
      self.lines_to_rip = 1

    try:
      self.lines_percent = int(self.parser.get(self.section, 'lines-percent'))
    except:
      self.lines_percent = 10
    
    try:
      self.crash_path = self.parser.get(self.section, 'crash-path')
    except:
      self.crash_path = None
    
    try:
      self.infinite_loop = self.parser.get(self.section, 'crash-path')
    except:
      self.infinite_loop = False

  def read_template(self, template):
    l = open(template, "rb").readlines()
    if self.strip_empty_lines:
      tmp = []
      for line in l:
        if line in ["\n", "\r\n"]:
          continue
        tmp.append(line)
      l = tmp
    self.template = l

  def minimize(self, template, outdir):
    self.read_template(template)

    log("Performing line-level test case minimization")
    start_at = os.getenv("NIGHTMARE_ITERATION")
    if start_at is not None:
      start_at = int(start_at)
      log("Starting from iteration %d\n" % start_at)
    else:
      start_at = 0

    self.do_try(outdir, start_at)

  def crash_file_exists(self):
    if self.crash_path is not None:
      return os.listdir(self.crash_path) > 0
    return False

  def remove_crash_path(self):
    if self.crash_path is not None:
      for f in os.listdir(self.crash_path):
        print "Removing", os.path.join(self.crash_path, f)
        os.remove(os.path.join(self.crash_path, f))

  def do_try(self, outdir, start_at=0):
    """ Try to remove a random number of lines iterating from the first
        line to the last one a number of times. Basically, we calculate
        a total number of lines to remove between 1 line and 10%. If the
        number of lines removed produces a test-case that still crashes,
        remove the lines from the template, otherwise, drop the changes 
        and move to the next line.

        IDEAS: Remove all empty lines before starting?
    """
    orig_lines = len(self.template)

    current_line = 0
    iteration = 0
    loops = 0
    while 1:
      self.minimized = False
      total_lines = len(self.template)
      log("Starting loop %d" % loops)
      current_line = 0

      for i in range(len(self.template)):
        self.read_configuration()
        log("Minimizing, iteration %d..." % iteration)
        iteration += 1
        temp_file = tempfile.mktemp(suffix=self.extension)
        lines = self.template

        if current_line >= len(lines):
          break

        if loops == 0 and not self.line_per_line:
          # Rip a random number of lines between 1 and self.lines_percent
          # but only at the very first iteration (when we remove most of
          # the stuff).
          val = (total_lines-current_line)*self.lines_percent/100
          if val == 0:
            val = 1

          lines_to_rip = random.randint(1, val)
          log("Removing %d line(s) (maximum of %d%%)" % (lines_to_rip, self.lines_percent))
        else:
          # For the likely final run remove only one line per try (or
          # whatever is specified in the configuration file)
          lines_to_rip = self.lines_to_rip
          log("Removing %d line(s)" % lines_to_rip)

        lines = lines[:current_line] + lines[current_line+lines_to_rip:]
        buf = "".join(lines)

        with open(temp_file, "wb") as f:
          f.write(buf)

        try:
          for key in self.env:
            os.putenv(key, self.env[key])

          self.remove_crash_path()

          if i % self.pre_iterations == 0:
            if self.pre_command is not None:
              log("Running pre-command %s" % self.pre_command)
              os.system(self.pre_command)

          if self.command.find("@@") == -1:
            cmd = "%s %s" % (self.command, temp_file)
          else:
            cmd = self.command.replace("@@", temp_file)
          ret = self.execute_command(cmd, self.timeout)

          if i % self.post_iterations == 0:
            if self.post_command is not None:
              log("Running post-command %s" % self.post_command)
              os.system(self.post_command)

          if ret in RETURN_SIGNALS or (self.signal is not None and ret == self.signal) or \
             self.crash_file_exists():
            self.template = lines
            log("Process crashed as expected...")
            buf = "".join(self.template)
            if not os.path.exists(outdir):
              log("Directory %s does not exists, creating it..." % outdir)
              os.mkdir(outdir)

            filename = os.path.join(outdir, "last_minimized%s" % self.extension)
            with open(filename, "wb") as f:
              f.write(buf)
            log("Last minimized test case %s written to disk." % filename)
          else:
            current_line += 1

          self.remove_crash_path()
        finally:
          os.remove(temp_file)

      loops += 1

      if len(self.template) == total_lines:
        log("File minimized from %d line(s) to %d line(s)" % (orig_lines, len(self.template)))
        buf = "".join(self.template)
        filename = sha1(buf).hexdigest()
        filename = os.path.join(outdir, "%s%s" % (filename, self.extension))
        with open(filename, "wb") as f:
          f.write(buf)
        log("Minimized test case %s written to disk." % filename)
        self.minimized = True
        break

#-----------------------------------------------------------------------
def main(mode, cfg, section, template, crash, diff, output=None):
  if mode == "generic":
    minimizer = CGenericMinimizer(cfg, section)
    minimizer.minimize(template, crash, diff, output)
  elif mode == "line":
    output = diff
    minimizer = CLineMinimizer(cfg, section)
    minimizer.minimize(template, output)
  else:
    print "Unknown mode '%s'" % mode

#-----------------------------------------------------------------------
def usage():
  print "Usage:", sys.argv[0], "<mode> <config file> <section> <template file> [<crashing file> <diff file>] <output directory>"
  print
  print "The value for 'mode' is either 'line' or 'generic'."
  print "The value for 'diff file' is only used for the 'generic' mode."

if __name__ == "__main__":
  if len(sys.argv) < 6:
    usage()
  elif len(sys.argv) == 6:
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[5])
  elif len(sys.argv) == 7:
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6])
