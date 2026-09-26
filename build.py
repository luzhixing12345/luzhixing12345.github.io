"""Build the static site into docs/ and optionally serve it.

    python build.py
    python build.py --build-only
"""

from gensite.builder import main

if __name__ == "__main__":
    main()
