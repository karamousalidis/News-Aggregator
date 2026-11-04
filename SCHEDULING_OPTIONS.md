# Scheduling options

## GitHub Actions: recommended

The [daily workflow](.github/workflows/digest.yml) runs in GitHub's cloud at
08:15 Europe/Athens. Follow the [README cloud setup](README.md#daily-cloud-setup).
Your Mac can stay off. Schedules are best effort; public-repository schedules
disable after 60 days without activity. Review failures in Actions.

## macOS: optional

Requires Python 3.12+. Configure `.env`, then:

```bash
python3 -m venv .venv
.venv/bin/python news_digest.py --dry-run --output preview.html
chmod +x schedule_macos.sh
./schedule_macos.sh
```

The launchd job runs at **08:15 local time daily**, using `.venv/bin/python`.
Logs are under `logs/`. It cannot run while the Mac is powered off.
Remove it before enabling cloud delivery:

```bash
./schedule_macos.sh --uninstall
```

## Windows: optional

Requires Python 3.12+. Configure `.env`, then use an appropriately privileged
PowerShell session:

```powershell
python -m venv .venv
powershell -ExecutionPolicy Bypass -File .\schedule.ps1
```

The task runs at **08:15 local time daily**, using `.venv\Scripts\python.exe`.
The computer must be available. Remove it with:

```powershell
powershell -ExecutionPolicy Bypass -File .\schedule.ps1 -Uninstall
```

Use `LOOKBACK_HOURS=24` for daily schedules. Equal 8-hour windows require
runs spaced 8 hours apart; 08:00, 14:00, and 20:00 leave an overnight gap.
