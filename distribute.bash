#!/usr/bin/env bash
rm ./project.zip
zip -r project.zip \
  active_inference_dynamic.py \
  dynamic_visualizer.py \
  flake.lock \
  flake.nix \
  requirements.txt \
  topology.py \
  state.json \
  ai/ \
  sdn/ \
  utils/ \
  --exclude "*/__pycache__/*" \
  --exclude "*.pyc"
echo "Done → project.zip"
