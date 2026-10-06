import sys

from critical_buildings.cli import main

if __name__ == "__main__":          # not on import: worker processes (the OpenStreetMap extract reader uses two) import this module too
    sys.exit(main())
