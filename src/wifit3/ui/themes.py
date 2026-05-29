
class WifitTheme:
    PRIMARY = "#00FF00"  # Matrix Green
    SECONDARY = "#008700"
    BACKGROUND = "#000000"
    ACCENT = "#FF0000"
    TEXT = "#FFFFFF"

    @classmethod
    def apply(cls, app):
        app.theme = "rich-black" # Just a placeholder if we want to use built-in base
