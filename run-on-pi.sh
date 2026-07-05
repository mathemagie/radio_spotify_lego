#!/usr/bin/env bash
# Launch LEGO Radio on the Nabaztag Pi from your Mac.
#
# The app is an ncurses TUI, so it needs a real terminal — `ssh -t` allocates
# one. Requires the `pi` SSH alias (see ~/.ssh/config) and the project already
# deployed to ~/radio_spotify_lego on the Pi.
#
# Usage: ./run-on-pi.sh

exec ssh -t pi '~/radio_spotify_lego/run.sh'
