# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This script exists to work around severe performane problems when WPA or other
Windows Performance Toolkit programs try to load the symbols for the Chrome
web browser. Some combination of the enormous size of the symbols or the
enhanced debug information generated by /Zo causes WPA to take about twenty
minutes to process the symbols for chrome.dll and chrome_child.dll. When
profiling Chrome this delay happens with every new set of symbols, so with
every new version of Chrome.

This script uses xperf actions to dump a list of the symbols referenced in
an ETW trace. If chrome.dll or chrome_child.dll is detected and if decoded
symbols are not found in %_NT_SYMCACHE_PATH% (default is c:\symcache) then
RetrieveSymbols.exe is used to download the symbols from the Chromium
symbol server, pdbcopy.exe is used to strip the private symbols, and then
another xperf action is used to load the stripped symbols, thus converting
them to .symcache files that can be efficiently loaded by WPA.

More details on the discovery of this slowness and the evolution of the fix
can be found here:
https://randomascii.wordpress.com/2014/11/04/slow-symbol-loading-in-microsofts-profiler-take-two/

Discussion and source code for RetrieveSymbols.exe can be found here:
https://randomascii.wordpress.com/2013/03/09/symbols-the-microsoft-way/

If "chromium-browser-symsrv" is not found in _NT_SYMBOL_PATH or RetrieveSymbols.exe
and pdbcopy.exe are not found then this script will exit early.
"""
from __future__ import print_function

import os
import sys
import re
import tempfile
import shutil
import subprocess

def run_and_look_for_matches(command):
  found_uncached = False
  # Typical output looks like:
  # "[RSDS] PdbSig: {be90dbc6-fe31-4842-9c72-7e2ea88f0adf}; Age: 1; Pdb: C:\b\build\slave\win\build\src\out\Release\syzygy\chrome.dll.pdb"
  pdb_re = re.compile(r'"\[RSDS\] PdbSig: {(.*-.*-.*-.*-.*)}; Age: (.*); Pdb: (.*)"')
  pdb_cached_re = re.compile(r"Found .*file - placed it in (.*)")
  
  #raw_command_output = subprocess.check_output(command)
  #command_output = str(raw_command_output).splitlines()
  # os.popen() is deprecated, but it *works*. Using subprocess leads to
  # "WindowsError: [Error 6] The handle is invalid" when running this script.
  command_output = os.popen(command).readlines()

  for line in command_output:
    if line.count("chrome.dll") > 0 or line.count("chrome_child.dll") > 0:
      match = pdb_re.match(line)
      if match:
        guid, age, path = match.groups()
        guid = guid.replace("-", "")
        filepart = os.path.split(path)[1]
        symcache_file = r"c:\symcache\chrome.dll-%s%sv2.symcache" % (guid, age)
        if os.path.exists(symcache_file):
          #print "Symcache file %s already exists. Skipping." % symcache_file
          continue
        # Only print messages for chrome PDBs that aren't in the symcache
        found_uncached = True
        print("Found uncached reference to %s: %s - %s" % (filepart, guid, age, ))
        symcache_files.append(symcache_file)
        pdb_cache_path = None
        retrieve_ommand = "%s %s %s %s" % (retrieve_path, guid, age, filepart)
        print("> %s" % retrieve_ommand)
        for subline in os.popen(retrieve_ommand):
          print(subline.strip())
          cache_match = pdb_cached_re.match(subline.strip())
          if cache_match:
            pdb_cache_path = cache_match.groups()[0]
        if not pdb_cache_path:
          # Look for locally built symbols
          if os.path.exists(path):
            pdb_cache_path = path
            local_symbol_files.append(path)
        if pdb_cache_path:
          tempdir = tempfile.mkdtemp()
          tempdirs.append(tempdir)
          dest_path = os.path.join(tempdir, os.path.split(pdb_cache_path)[1])
          print("Copying PDB to %s" % dest_path)
          for copyline in os.popen("%s %s %s -p" % (pdbcopy_path, pdb_cache_path, dest_path)):
            print(copyline.strip())
        else:
          print("Failed to retrieve symbols. Check for RetrieveSymbols.exe and support files.")
  return found_uncached


def main():
  if len(sys.argv) < 2:
    print("Usage: %s trace.etl" % sys.argv[0])
    sys.exit(0)

  symbol_path = os.environ.get("_NT_SYMBOL_PATH", "")
  if symbol_path.count("chromium-browser-symsrv") == 0:
    print("Chromium symbol server is not in _NT_SYMBOL_PATH. No symbol stripping needed.")
    sys.exit(0)

  script_dir = os.path.split(sys.argv[0])[0]
  retrieve_path = os.path.join(script_dir, "RetrieveSymbols.exe")
  pdbcopy_path = os.path.join(script_dir, "pdbcopy.exe")

  # RetrieveSymbols.exe requires some support files. dbghelp.dll and symsrv.dll
  # have to be in the same directory as RetrieveSymbols.exe and pdbcopy.exe must
  # be in the path, so copy them all to the script directory.
  for third_party in ["pdbcopy.exe", "dbghelp.dll", "symsrv.dll"]:
    if not os.path.exists(third_party):
      source = os.path.normpath(os.path.join(script_dir, r"..\third_party", \
          third_party))
      dest = os.path.normpath(os.path.join(script_dir, third_party))
      shutil.copy2(source, dest)

  if not os.path.exists(pdbcopy_path):
    print("pdbcopy.exe not found. No symbol stripping is possible.")
    sys.exit(0)

  if not os.path.exists(retrieve_path):
    print("RetrieveSymbols.exe not found. No symbol retrieval is possible.")
    sys.exit(0)

  tracename = sys.argv[1]
  # Each symbol file that we pdbcopy gets copied to a separate directory so
  # that we can support decoding symbols for multiple chrome versions without
  # filename collisions.
  tempdirs = []


  print("Pre-translating chrome symbols from stripped PDBs to avoid 10-15 minute translation times.")

  symcache_files = []
  # Keep track of the local symbol files so that we can temporarily rename them
  # to stop xperf from using -- rename them from .pdb to .pdbx
  local_symbol_files = []

  command = 'xperf -i "%s" -tle -tti -a symcache -dbgid' % tracename
  print("> %s" % command)
  found_uncached = run_and_look_for_matches(command)


  if tempdirs:
    symbol_path = ";".join(tempdirs)
    print("Stripped PDBs are in %s. Converting to symcache files now." % symbol_path)
    os.environ["_NT_SYMBOL_PATH"] = symbol_path
    for local_pdb in local_symbol_files:
      temp_name = local_pdb + "x"
      print("Renaming %s to %s to stop unstripped PDBs from being used." % (local_pdb, temp_name))
      os.rename(local_pdb, temp_name)

    gen_command = 'xperf -i "%s" -symbols -tle -tti -a symcache -build' % tracename
    print("> %s" % gen_command)
    for line in os.popen(gen_command).readlines():
      pass # Don't print line

    for local_pdb in local_symbol_files:
      temp_name = local_pdb + "x"
      os.rename(temp_name, local_pdb)
    
    error = False
    for symcache_file in symcache_files:
      if os.path.exists(symcache_file):
        print("%s generated." % symcache_file)
      else:
        print("Error: %s not generated." % symcache_file)
        error = True
    # Delete the stripped PDB files
    if error:
      print("Retaining PDBs to allow rerunning xperf command-line.")
    else:
      for directory in tempdirs:
        shutil.rmtree(directory, ignore_errors=True)
  else:
    if found_uncached:
      print("No PDBs copied, nothing to do.")
    else:
      print("No uncached PDBS found, nothing to do.")

if __name__ == "__main__":
  main()
