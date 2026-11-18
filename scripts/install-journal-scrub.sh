#!/bin/bash
# Renamed: this script removes the old journal scrub helper, it installs nothing.
exec bash "$(dirname "${BASH_SOURCE[0]}")/retire-journal-scrub.sh" "$@"
