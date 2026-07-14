# TimeLapse application icons

The build scripts use these assets automatically:

- `timelapse.icns` — native macOS application bundle
- `timelapse.ico` — native Windows WPF executable
- `timelapse.png` — 1024×1024 RGBA master and Linux/Qt window icon
- `png/` — common PNG sizes for launchers and packaging

Set `TIMELAPSE_ICON` to override the platform-specific build icon. The bundled
Qt window icon continues to use the PNG master so it renders consistently on
Windows and Linux.
