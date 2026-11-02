"""PyInstaller entry point: run the desktop app, with the data folder next to the user's home as usual."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--server" in sys.argv:  # headless: just the server, for people who prefer their own browser
        from tillu.__main__ import main
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--server"]
        main()
    else:
        from tillu.desktop import main
        main()
