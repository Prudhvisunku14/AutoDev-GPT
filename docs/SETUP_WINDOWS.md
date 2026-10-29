# DevPilot AI: Phase 3 setup on this laptop (WSL2 + Docker Desktop)

> Prepared 2026-10-29. **Nothing here has been installed yet.** Every step marked *(admin)* needs your administrator approval and is run by you, not by Claude Code.

## Target locations

| What | Where |
|---|---|
| Project | `D:\DOWNLOAND_NEW_D\AI\AI` |
| Python | `D:\python.exe` (venv in `backend\.venv`) |
| Docker Desktop program | `D:\DockerDesktop` |
| Docker data (images, containers, volumes) | `D:\DockerData` |
| WSL swap file | `D:\WSL\swap.vhdx` |
| Downloads, temp files and caches | `D:\DevPilotCache` |

## What must stay on C: (cannot be moved)

- **Windows features**: *Virtual Machine Platform* and the WSL subsystem are part of Windows (`C:\Windows`).
- **The WSL package** (`wsl.exe`, Linux kernel, WSLg) installs to `C:\Program Files\WSL`. Estimate: under 1 GB.
- **Docker Desktop's per-user files**, even with the program on D:
  - `%APPDATA%\Docker` (settings)
  - `%LOCALAPPDATA%\Docker` (logs, which can grow to a few hundred MB)
  - `%USERPROFILE%\.docker` (CLI config, contexts, buildx)
  - `C:\ProgramData\DockerDesktop` (install logs)
- **`%USERPROFILE%\.wslconfig`**, a tiny text file.
- **Headroom for Windows itself**: updates, the feature install, the reboot, the page file. Installer temp files also land on C: unless TEMP is redirected, as shown in step 5.

Everything large goes to D:: the Docker disk image (`D:\DockerData`), the WSL swap file, and the installer download. No Linux distribution is installed on C:, because step 2 uses `--no-distribution`.

**Free space needed on C: before starting: at least 15 GB.** On 2026-10-29 it had 0.43 GB. Candidates to review are listed at the end.

## Installation sequence

**Before you start:** free space on C: until it has at least 15 GB free. Then check that Task Manager → Performance → CPU shows "Virtualization: Enabled". (This laptop already reports that a hypervisor is running, which implies it is enabled.)

**1. Prepare folders.** Run this in a normal PowerShell:
   ```powershell
   New-Item -ItemType Directory -Force D:\DockerDesktop, D:\DockerData, D:\WSL, D:\DevPilotCache\installers
   ```

**2. Install the WSL platform only *(admin)*.** Use an elevated PowerShell:
   ```powershell
   wsl --install --no-distribution
   ```
   Reboot when asked. Then, in a normal PowerShell:
   ```powershell
   wsl --update
   wsl --version          # expect WSL version 2.x
   wsl --status           # default version should be 2
   ```

**3. Limit WSL and move its swap file to D:.** Create `%USERPROFILE%\.wslconfig` with:
   ```ini
   [wsl2]
   memory=6GB
   processors=4
   swap=4GB
   swapFile=D:\\WSL\\swap.vhdx

   [experimental]
   sparseVhd=true
   autoMemoryReclaim=gradual
   ```
   - 6 GB leaves about 9 GB for Windows on this 15.7 GB laptop.
   - `sparseVhd` lets new virtual disks shrink when space is freed.
   - Apply the file with `wsl --shutdown`.

**4. Download the Docker Desktop installer.** Get it from docker.com (Docker Desktop for Windows, AMD64) and save it as `D:\DevPilotCache\installers\Docker Desktop Installer.exe`. Docker Desktop is free for personal and educational use, and for small businesses under Docker's terms.

**5. Install Docker Desktop to D: *(admin)*.** Run this in an elevated PowerShell:
   ```powershell
   $env:TEMP = 'D:\DevPilotCache\temp'; $env:TMP = $env:TEMP     # keep installer temp files off C:
   Start-Process -Wait -FilePath 'D:\DevPilotCache\installers\Docker Desktop Installer.exe' -ArgumentList `
     'install', '--accept-license', '--backend=wsl-2', `
     '--installation-dir=D:\DockerDesktop', '--wsl-default-data-root=D:\DockerData'
   ```

**6. Sign out and back in.** This picks up the `docker-users` group membership.

**7. Start Docker Desktop for the first time.**
   - Run `D:\DockerDesktop\Docker Desktop.exe`. Signing in to a Docker account is optional.
   - Check Settings → Resources → Advanced → *Disk image location* = `D:\DockerData`.
   - Optionally clear *Start Docker Desktop when you sign in* to save memory when you are not developing.

**8. Verify.** Run these in a normal PowerShell:
   ```powershell
   docker version
   wsl -l -v                         # docker-desktop, VERSION 2
   docker run --rm hello-world       # official test image (about 20 KB)
   Get-ChildItem D:\DockerData       # the Docker disk image is here, not on C:
   ```
   Then compare C: free space with the number before step 2.

**9. Tell Claude Code it is installed.** Phase 3 starts only after your explicit approval.

## Freeing space on C: (your decision; nothing has been deleted)

Measured 2026-10-29. All of the following can be regenerated. Clear them with each tool's own command rather than by deleting folders:

| GB | Location | How to clear |
|---|---|---|
| 2.84 | `%USERPROFILE%\.cache` | Look inside first: it may hold downloaded models you still want |
| 1.45 | `%LOCALAPPDATA%\pip\Cache` | `py -3.11 -m pip cache purge` |
| 1.31 | `%LOCALAPPDATA%\npm-cache` | `npm cache clean --force` |
| 1.01 | `%LOCALAPPDATA%\uv` | `uv cache clean` (if uv is installed) |
| 0.51 | `%LOCALAPPDATA%\Temp` | Settings → System → Storage → Temporary files |

Larger items worth reviewing:

| GB | Location | What it is |
|---|---|---|
| 10.57 | `%LOCALAPPDATA%\Packages` | Microsoft Store app data |
| 6.49 | `%LOCALAPPDATA%\Programs` | Per-user programs; uninstall unused ones |
| 5.55 | `%LOCALAPPDATA%\Google` | Mostly Chrome profile and cache; clear browsing data in Chrome |
| 2.04 + 1.03 | WebEx, CiscoSparkLauncher | Uninstall if unused |

Also run Settings → System → Storage → *Cleanup recommendations*, and Disk Cleanup → *Clean up system files*.

The project's own caches are already on D: (`D:\DevPilotCache`).
