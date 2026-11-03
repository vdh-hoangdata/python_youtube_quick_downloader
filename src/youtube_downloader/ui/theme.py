"""Shared light colors, independent of the Windows light/dark preference."""
from PySide6.QtGui import QColor, QPalette


def light_palette() -> QPalette:
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: '#f8f9fb',
        QPalette.ColorRole.WindowText: '#20242c',
        QPalette.ColorRole.Base: '#ffffff',
        QPalette.ColorRole.AlternateBase: '#f1f5f9',
        QPalette.ColorRole.Text: '#20242c',
        QPalette.ColorRole.Button: '#ffffff',
        QPalette.ColorRole.ButtonText: '#20242c',
        QPalette.ColorRole.Highlight: '#dbeafe',
        QPalette.ColorRole.HighlightedText: '#17365d',
        QPalette.ColorRole.ToolTipBase: '#ffffff',
        QPalette.ColorRole.ToolTipText: '#20242c',
        QPalette.ColorRole.PlaceholderText: '#626e7e',
        QPalette.ColorRole.Light: '#ffffff',
        QPalette.ColorRole.Midlight: '#eef2f7',
        QPalette.ColorRole.Mid: '#cbd2dc',
        QPalette.ColorRole.Dark: '#64748b',
        QPalette.ColorRole.Shadow: '#64748b',
        QPalette.ColorRole.BrightText: '#ffffff',
        QPalette.ColorRole.Link: '#1456a0',
        QPalette.ColorRole.LinkVisited: '#633c92',
    }
    for role, value in colors.items():
        palette.setColor(role, QColor(value))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText, QPalette.ColorRole.PlaceholderText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor('#586270'))
    for role in (QPalette.ColorRole.Base, QPalette.ColorRole.Button):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor('#e9ecf1'))
    return palette
