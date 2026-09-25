#!/usr/bin/env bash
rm -f ./project.zip
zip -r project.zip \
  app.py \
  active_inference_dynamic.py \
  control_panel.py \
  dynamic_visualizer.py \
  process_manager.py \
  topology.py \
  topology_editor.py \
  traffic_manager.py \
  traffic_panel.py \
  flake.lock \
  flake.nix \
  requirements.txt \
  README.md \
  ai/ \
  sdn/ \
  utils/ \
  --exclude "*/__pycache__/*" \
  --exclude "*.pyc"

# Generated at runtime — include a snapshot if present, skip quietly if not
# (fresh checkouts won't have these until the app has been run once).
for f in topology_spec.json state.json; do
  if [ -f "$f" ]; then
    zip project.zip "$f"
  fi
done

echo "Done → project.zip"
