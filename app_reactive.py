#!/usr/bin/env python3
"""
app_reactive.py — Single-process orchestrator, pre-configured for the
Reactive (threshold-based) congestion-control backend.

This is the exact same integrated app as app.py — same Topology Editor,
Control Panel, Traffic Generator, and Logs Console dock, same child-process
management for the controller and Mininet network. You never run
`ryu-manager` yourself; the Control Panel's Start button spawns it as a
managed child process (see process_manager.py), exactly like app.py does.
The only difference here is which routing algorithm is selected by
default when the window opens: **Reactive (threshold-based)**
(`reactive_dynamic.py`) instead of Active Inference.

The Control Panel's "Routing algorithm" dropdown still lets you switch to
Active Inference from this window too — and the dropdown in app.py can
switch the other way — so you only need whichever one of these two files
is more convenient to have as your default; both give you both algorithms.

Run it with:
    python3 app_reactive.py
"""

from app import main

if __name__ == "__main__":
    main(default_controller_module="reactive_dynamic.py")
