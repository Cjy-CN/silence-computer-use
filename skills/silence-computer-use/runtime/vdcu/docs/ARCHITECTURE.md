# Architecture

```text
vd_cu.py -> session manager -> signed shadow host -> isolated Windows session
                  |                 |                       |
              lifecycle       SDK render surface        cu.exe agent
                                                            |
cu_bridge.py <------- dedicated named pipe ------------------+
```

`client.py`, `session.py`, and `desktop.py` manage daemon/worker lifecycle.
`authorization.py` checks empty local credentials before selecting the UI path.
`shadow_host.py` owns a message-pumping UI thread and implements worker operations.
`shadow_engine.py` binds the native SDK; the native binaries stay unchanged.
`cu_bridge.py` drives the in-session GUI agent with window-scoped UIA and screenshots.

The preview and optional authorization card belong to the host desktop, but GUI
input is delivered by the agent inside the independent session. The active user
desktop is never switched for tasks. Files and clipboard are not isolated.

Creation must yield a rendered frame, then the agent must respond on its dedicated
pipe. Shutdown destroys the owned session. Cleanup must not kill unrelated apps
based only on a generic window title.
