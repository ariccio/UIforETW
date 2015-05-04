/*
Copyright 2015 Google Inc. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

#include "stdafx.h"
#include <ETWProviders\etwprof.h>
#define PSAPI_VERSION 1
#include <psapi.h>
#include <vector>
#include <cstdint>
#include <TlHelp32.h>
#include "Utility.h"
#include "WorkingSet.h"

#pragma comment(lib, "psapi.lib")

const DWORD kSamplingInterval = 1000;

void CWorkingSetMonitor::SampleWorkingSets()
{
	CSingleLock locker(&processesLock_);
	if (processes_.empty() && !processAll_)
		return;

	// CreateToolhelp32Snapshot runs faster than EnumProcesses and
	// it returns the process name as well, thus avoiding a call to
	// EnumProcessModules to get the name.
	HANDLE hSnapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, TH32CS_SNAPPROCESS);
	if (!hSnapshot)
		return;

	PROCESSENTRY32W peInfo;
	peInfo.dwSize = sizeof(peInfo);
	BOOL nextProcess = Process32First(hSnapshot, &peInfo);

	// Allocate enough space to get the working set of most processes.
	// It will grow if needed.
	ULONG_PTR numEntries = 100000;
	std::vector<char> buffer(sizeof(PSAPI_WORKING_SET_INFORMATION) + numEntries * sizeof(PSAPI_WORKING_SET_BLOCK));
	PSAPI_WORKING_SET_INFORMATION* pwsBuffer = reinterpret_cast<PSAPI_WORKING_SET_INFORMATION*>(&buffer[0]);

	unsigned totalWSPages = 0;
	// The PSS page count is stored as a multiple of PSSMultiplier.
	// This allows all the supported share counts, from 1 to 7, to be
	// divided out without loss of precision. That is, an unshared page
	// is recorded by adding 420. A page shared by seven processes (the
	// maximum recorded) is recorded by adding 420/7.
	const uint64_t PSSMultiplier = 420; // LCM of 1, 2, 3, 4, 5, 6, 7
	uint64_t totalPSSPages = 0;
	unsigned totalPrivateWSPages = 0;

	// Iterate through the processes.
	while (nextProcess)
	{
		bool match = processAll_;
		for (const auto& name : processes_)
		{
			if (_wcsicmp(peInfo.szExeFile, name.c_str()) == 0)
				match = true;
		}
		if (match)
		{
			DWORD pid = peInfo.th32ProcessID;
			// Get a handle to the process.
			HANDLE hProcess = OpenProcess(PROCESS_QUERY_INFORMATION |
				PROCESS_VM_READ, FALSE, pid);

			if (NULL != hProcess)
			{
				bool success = true;
				if (!QueryWorkingSet(hProcess, &buffer[0], buffer.size()))
				{
					// Increase the buffer size based on the NumberOfEntries returned,
					// with some padding in case the working set is increasing.
					if (GetLastError() == ERROR_BAD_LENGTH)
						numEntries = pwsBuffer->NumberOfEntries + pwsBuffer->NumberOfEntries / 4;
					buffer.resize(sizeof(PSAPI_WORKING_SET_INFORMATION) + numEntries * sizeof(PSAPI_WORKING_SET_BLOCK));
					pwsBuffer = reinterpret_cast<PSAPI_WORKING_SET_INFORMATION*>(&buffer[0]);
					if (!QueryWorkingSet(hProcess, &buffer[0], buffer.size()))
					{
						success = false;
					}
				}

				if (success)
				{
					ULONG_PTR wsPages = pwsBuffer->NumberOfEntries;
					uint64_t PSSPages = 0;
					ULONG_PTR privateWSPages = 0;
					for (ULONG_PTR page = 0; page < wsPages; ++page)
					{
						if (!pwsBuffer->WorkingSetInfo[page].Shared)
						{
							++privateWSPages;
							PSSPages += PSSMultiplier;
						}
						else
						{
							assert(pwsBuffer->WorkingSetInfo[page].ShareCount <= 7);
							PSSPages += PSSMultiplier / pwsBuffer->WorkingSetInfo[page].ShareCount;
						}
					}
					totalWSPages += wsPages;
					totalPSSPages += PSSPages;
					totalPrivateWSPages += privateWSPages;

					wchar_t process[MAX_PATH + 100];
					swprintf_s(process, L"%s (%u)", peInfo.szExeFile, pid);
					ETWMarkWorkingSet(peInfo.szExeFile, process, privateWSPages * 4, (unsigned)((PSSPages * 4) / PSSMultiplier), wsPages * 4);
				}

				CloseHandle(hProcess);
			}
		}
		nextProcess = Process32Next(hSnapshot, &peInfo);
	}
	CloseHandle(hSnapshot);

	ETWMarkWorkingSet(L"Total", L"", totalPrivateWSPages * 4, (unsigned)((totalPSSPages * 4) / PSSMultiplier), totalWSPages * 4);
}

DWORD __stdcall CWorkingSetMonitor::StaticWSMonitorThread(LPVOID param)
{
	CWorkingSetMonitor* pThis = reinterpret_cast<CWorkingSetMonitor*>(param);
	pThis->WSMonitorThread();
	return 0;
}

void CWorkingSetMonitor::WSMonitorThread()
{

	for (;;)
	{
		DWORD result = WaitForSingleObject(hExitEvent_, kSamplingInterval);
		if (result == WAIT_OBJECT_0)
			break;

		SampleWorkingSets();
	}
}

CWorkingSetMonitor::CWorkingSetMonitor()
{
	hExitEvent_ = CreateEvent(nullptr, FALSE, FALSE, nullptr);
	hThread_ = CreateThread(NULL, 0, StaticWSMonitorThread, this, 0, NULL);
}

CWorkingSetMonitor::~CWorkingSetMonitor()
{
	// Shut down the child thread.
	SetEvent(hExitEvent_);
	WaitForSingleObject(hThread_, INFINITE);
	CloseHandle(hThread_);
	CloseHandle(hExitEvent_);
}

void CWorkingSetMonitor::SetProcessFilter(const std::wstring& processes)
{
	CSingleLock locker(&processesLock_);
	if (processes == L"*")
	{
		processAll_ = true;
	}
	else
	{
		processAll_ = false;
		processes_ = split(processes, ';');
	}
}
