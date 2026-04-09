const logBox = document.getElementById("logBox");
const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");

const appendLog = (line) => {
  const ts = new Date().toLocaleTimeString();
  logBox.textContent += `[${ts}] ${line}\n`;
  logBox.scrollTop = logBox.scrollHeight;
};

const getNum = (id) => Number.parseFloat(document.getElementById(id).value);
const getText = (id) => document.getElementById(id).value.trim();
const getBool = (id) => document.getElementById(id).checked;

const getConfig = () => ({
  sample_rate: 16000,
  chunk_seconds: getNum("chunkSeconds"),
  duck_percent: getNum("duckPercent"),
  hold_seconds: getNum("holdSeconds"),
  min_rms: getNum("minRms"),
  model_size: getText("modelSize"),
  language: getText("language") || "en",
  profanity_file: getText("profanityFile"),
  input_source: getText("inputSource") || "loopback",
  input_device: getText("inputDevice"),
  lyrics_mode: getBool("lyricsMode"),
  spotify_token: getText("spotifyToken"),
  playlist_id: getText("playlistId"),
  prefetch_playlist_lyrics: getBool("prefetchPlaylistLyrics"),
  lyrics_preduck_seconds: getNum("lyricsPreduckSeconds"),
  import_playlist_csv_paths: getText("playlistCsvPaths"),
  prefetch_csv_lyrics: getBool("prefetchCsvLyrics"),
  prefetch_only: getBool("prefetchOnly"),
});

const setRunning = (running) => {
  startBtn.disabled = running;
  stopBtn.disabled = !running;
};

const isWarningText = (text) => {
  const normalized = String(text).toLowerCase();
  return normalized.includes("warning") || normalized.includes("userwarning");
};

const boot = async () => {
  const tauri = window.__TAURI__;

  if (!tauri || !tauri.core || !tauri.event) {
    appendLog("Tauri API is unavailable. Run inside Tauri.");
    startBtn.disabled = true;
    stopBtn.disabled = true;
    return;
  }

  const { invoke } = tauri.core;
  const { listen } = tauri.event;

  await listen("engine-log", (event) => appendLog(String(event.payload)));
  await listen("engine-error", (event) => {
    const line = String(event.payload);
    if (isWarningText(line)) {
      appendLog(`WARN: ${line}`);
      return;
    }

    appendLog(`ERROR: ${line}`);
  });
  await listen("engine-stopped", (event) => {
    appendLog(String(event.payload));
    setRunning(false);
  });

  startBtn.addEventListener("click", async () => {
    try {
      appendLog("Starting monitor...");
      await invoke("start_monitor", { config: getConfig() });
      setRunning(true);
    } catch (err) {
      appendLog(`ERROR: ${String(err)}`);
      setRunning(false);
    }
  });

  stopBtn.addEventListener("click", async () => {
    try {
      await invoke("stop_monitor");
      appendLog("Stop requested.");
      setRunning(false);
    } catch (err) {
      appendLog(`ERROR: ${String(err)}`);
    }
  });
};

boot().catch((err) => {
  appendLog(`ERROR: ${String(err)}`);
});
