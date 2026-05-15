'use strict';

const { app, BrowserWindow } = require('electron');

const VIEWER_URL = process.env.VIEWER_URL || 'http://localhost:8080/';
const FULLSCREEN = process.env.VIEWER_FULLSCREEN === '1';
const KIOSK = process.env.VIEWER_KIOSK === '1';

// VaapiOnNvidiaGPUs lifts Chromium's NVIDIA gate. VaapiIgnoreDriverChecks
// stops it from rejecting nvidia-vaapi-driver based on the libva vendor
// string. VaapiVideoDecodeLinuxGL routes decoded surfaces through the GL
// compositor (required when use-gl=angle / use-angle=gl-egl).
app.commandLine.appendSwitch('enable-features', [
  'VaapiVideoDecoder',
  'VaapiVideoDecodeLinuxGL',
  'VaapiOnNvidiaGPUs',
  'VaapiIgnoreDriverChecks',
  'AcceleratedVideoDecodeLinuxGL',
].join(','));

app.commandLine.appendSwitch('disable-features',
  'UseChromeOSDirectVideoDecoder');

app.commandLine.appendSwitch('use-gl', 'angle');
app.commandLine.appendSwitch('use-angle', 'gl-egl');
app.commandLine.appendSwitch('ignore-gpu-blocklist');
app.commandLine.appendSwitch('enable-accelerated-video-decode');
app.commandLine.appendSwitch('ozone-platform', 'x11');

// chrome-sandbox requires a setuid helper we don't ship in the container.
app.commandLine.appendSwitch('no-sandbox');

app.whenReady().then(() => {
  const win = new BrowserWindow({
    width: 1920,
    height: 1080,
    fullscreen: FULLSCREEN || KIOSK,
    kiosk: KIOSK,
    autoHideMenuBar: true,
    backgroundColor: '#000000',
    show: false,
    webPreferences: {
      backgroundThrottling: false,
      sandbox: false,
    },
  });

  win.once('ready-to-show', () => win.show());
  win.loadURL(VIEWER_URL);
});

app.on('window-all-closed', () => app.quit());
