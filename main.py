"""Application entry point. UI implementations live under :mod:`ui`."""

from ui import ChatApp


def main():
    app = ChatApp()
    app.mainloop()


if __name__ == "__main__":
    main()
