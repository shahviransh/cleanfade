import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

const defaultConfig = {
  chunkSeconds: 2.0,
  duckPercent: 45,
  holdSeconds: 2.5,
  minRms: 0.002,
  modelSize: "medium",
  language: "en",
  profanityFile: "",
  inputSource: "loopback",
  inputDevice: "",
  lyricsMode: false,
  hfToken: "",
  spotifyToken: "",
  playlistId: "",
  prefetchPlaylistLyrics: false,
  lyricsPreduckSeconds: 0.8,
  playlistCsvPaths: "",
  prefetchCsvLyrics: true,
  prefetchOnly: false,
  csvExportMode: true,
};

const isWarningText = (text) => {
  const normalized = String(text).toLowerCase();
  return normalized.includes("warning") || normalized.includes("userwarning");
};

const formatConfig = (form) => ({
  sample_rate: 16000,
  chunk_seconds: Number.parseFloat(form.chunkSeconds),
  duck_percent: Number.parseFloat(form.duckPercent),
  hold_seconds: Number.parseFloat(form.holdSeconds),
  min_rms: Number.parseFloat(form.minRms),
  model_size: String(form.modelSize || "small"),
  language: String(form.language || "en").trim() || "en",
  profanity_file: String(form.profanityFile || "").trim(),
  input_source: String(form.inputSource || "loopback"),
  input_device: String(form.inputDevice || "").trim(),
  lyrics_mode: Boolean(form.lyricsMode),
  hf_token: String(form.hfToken || "").trim(),
  spotify_token: String(form.spotifyToken || "").trim(),
  playlist_id: String(form.playlistId || "").trim(),
  prefetch_playlist_lyrics: Boolean(form.prefetchPlaylistLyrics),
  lyrics_preduck_seconds: Number.parseFloat(form.lyricsPreduckSeconds),
  import_playlist_csv_paths: String(form.playlistCsvPaths || "").trim(),
  prefetch_csv_lyrics: Boolean(form.prefetchCsvLyrics),
  prefetch_only: Boolean(form.prefetchOnly),
});

function App() {
  const [form, setForm] = useState(defaultConfig);
  const [running, setRunning] = useState(false);
  const [logLines, setLogLines] = useState([]);
  const [tauriReady, setTauriReady] = useState(false);
  const logRef = useRef(null);

  const appendLog = (line) => {
    const ts = new Date().toLocaleTimeString();
    setLogLines((current) => [...current, `[${ts}] ${line}`]);
  };

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [logLines]);

  useEffect(() => {
    if (!window.__TAURI_INTERNALS__) {
      appendLog("Tauri API is unavailable. Run inside Tauri.");
      return undefined;
    }

    let unlistenFns = [];
    let disposed = false;

    const setupListeners = async () => {
      try {
        const unlistenLog = await listen("engine-log", (event) => appendLog(String(event.payload)));
        const unlistenErr = await listen("engine-error", (event) => {
          const line = String(event.payload);
          if (isWarningText(line)) {
            appendLog(`WARN: ${line}`);
            return;
          }
          appendLog(`ERROR: ${line}`);
        });
        const unlistenStopped = await listen("engine-stopped", (event) => {
          appendLog(String(event.payload));
          setRunning(false);
        });

        if (disposed) {
          unlistenLog();
          unlistenErr();
          unlistenStopped();
          return;
        }

        unlistenFns = [unlistenLog, unlistenErr, unlistenStopped];
        setTauriReady(true);
      } catch (err) {
        appendLog(`ERROR: ${String(err)}`);
        setTauriReady(false);
      }
    };

    setupListeners();

    return () => {
      disposed = true;
      for (const unlistenFn of unlistenFns) {
        unlistenFn();
      }
    };
  }, []);

  useEffect(() => {
    if (!tauriReady) {
      return undefined;
    }

    const onBeforeUnload = () => {
      invoke("stop_monitor").catch(() => {});
    };

    window.addEventListener("beforeunload", onBeforeUnload);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
    };
  }, [tauriReady]);

  const onText = (field) => (event) => {
    const value = event.target.value;
    setForm((current) => ({ ...current, [field]: value }));
  };

  const onBool = (field) => (event) => {
    const value = event.target.checked;
    setForm((current) => ({ ...current, [field]: value }));
  };

  const startMonitor = async () => {
    if (!tauriReady) {
      appendLog("ERROR: Tauri is not ready.");
      return;
    }

    try {
      appendLog("Starting monitor...");
      await invoke("start_monitor", { config: formatConfig(form) });
      setRunning(true);
    } catch (err) {
      appendLog(`ERROR: ${String(err)}`);
      setRunning(false);
    }
  };

  const stopMonitor = async () => {
    if (!tauriReady) {
      appendLog("ERROR: Tauri is not ready.");
      return;
    }

    try {
      await invoke("stop_monitor");
      appendLog("Stop requested.");
      setRunning(false);
    } catch (err) {
      appendLog(`ERROR: ${String(err)}`);
    }
  };

  return (
    <>
      <div className="bg-glow"></div>
      <main className="shell">
        <header>
          <p className="eyebrow">Desktop profanity guard</p>
          <h1>CleanFade</h1>
          <p className="subtext">Reduce Spotify volume whenever curse words are detected.</p>
        </header>

        <section className="grid">
          <label>
            <span className="label-title">
              Duck percent
              <span className="help-dot" tabIndex={0} data-tip="How much to lower Spotify volume when profanity is detected.">?</span>
            </span>
            <input type="number" min="0" max="100" step="1" value={form.duckPercent} onChange={onText("duckPercent")} />
          </label>

          <label>
            <span className="label-title">
              Hold seconds
              <span className="help-dot" tabIndex={0} data-tip="How long volume stays reduced after the last detected profanity.">?</span>
            </span>
            <input type="number" min="0.5" max="10" step="0.1" value={form.holdSeconds} onChange={onText("holdSeconds")} />
          </label>

          <label>
            <span className="label-title">
              Chunk seconds
              <span className="help-dot" tabIndex={0} data-tip="Audio chunk length for transcription. Lower values react faster, higher values can be more stable.">?</span>
            </span>
            <input type="number" min="0.5" max="5" step="0.1" value={form.chunkSeconds} onChange={onText("chunkSeconds")} />
          </label>

          <label>
            <span className="label-title">
              Min RMS
              <span className="help-dot" tabIndex={0} data-tip="Ignore very quiet audio below this loudness threshold.">?</span>
            </span>
            <input type="number" min="0" max="1" step="0.0005" value={form.minRms} onChange={onText("minRms")} />
          </label>

          <label>
            <span className="label-title">
              Whisper model
              <span className="help-dot" tabIndex={0} data-tip="Speech model size. small/medium are more accurate but slower than tiny/base.">?</span>
            </span>
            <select value={form.modelSize} onChange={onText("modelSize")}>
              <option>tiny</option>
              <option>base</option>
              <option>small</option>
              <option>medium</option>
              <option>large-v1</option>
              <option>large-v2</option>
              <option>large-v3</option>
            </select>
          </label>

          <label>
            <span className="label-title">
              Language code
              <span className="help-dot" tabIndex={0} data-tip="Language passed to transcription, for example en, es, or fr.">?</span>
            </span>
            <input type="text" value={form.language} onChange={onText("language")} />
          </label>

          <label>
            <span className="label-title">
              Input source
              <span className="help-dot" tabIndex={0} data-tip="Use loopback to capture speaker output (recommended for Spotify), or microphone for room/voice input.">?</span>
            </span>
            <select value={form.inputSource} onChange={onText("inputSource")}>
              <option value="loopback">loopback</option>
              <option value="microphone">microphone</option>
            </select>
          </label>

          <label>
            <span className="label-title">
              Input device (optional)
              <span className="help-dot" tabIndex={0} data-tip="Partial device name to force a specific source, e.g. Headset Earphone or Living Room speaker.">?</span>
            </span>
            <input type="text" placeholder="Headset Earphone" value={form.inputDevice} onChange={onText("inputDevice")} />
          </label>

          <label className="wide">
            <span className="label-title">
              Custom profanity file (optional)
              <span className="help-dot" tabIndex={0} data-tip="Optional path to a text file with one profanity word per line.">?</span>
            </span>
            <input type="text" placeholder="C:/path/to/words.txt" value={form.profanityFile} onChange={onText("profanityFile")} />
          </label>

          <label className="wide">
            <span className="label-title">
              <span>Lyrics predictive ducking (Spotify token mode)</span>
              <span className="help-dot" tabIndex={0} data-tip="Optional advanced mode. Uses Spotify playback progress + synced lyrics to duck before a profane line starts.">?</span>
            </span>
            <input type="checkbox" checked={form.lyricsMode} onChange={onBool("lyricsMode")} />
          </label>

          <label className="wide">
            <span className="label-title">
              <span>Not using Spotify token (CSV export mode)</span>
              <span className="help-dot" tabIndex={0} data-tip="Use this mode if you do not have a Spotify token. Export your songs to CSV first, then paste CSV paths below.">?</span>
            </span>
            <input type="checkbox" checked={form.csvExportMode} onChange={onBool("csvExportMode")} />
          </label>

          {form.csvExportMode ? (
            <div className="wide guidance-box">
              <p>If you are not using a Spotify token, export your songs to CSV first.</p>
              <p>Spotify playlists: use Exportify. Apple Music, YouTube Music, and other platforms: use TuneMyMusic.</p>
            </div>
          ) : null}

          <label className="wide">
            <span className="label-title">
              Playlist CSV paths
              <span className="help-dot" tabIndex={0} data-tip="Paste one or more CSV file paths (one per line). Supports Exportify and TuneMyMusic formats.">?</span>
            </span>
            <textarea rows="3" placeholder={"C:/path/your-songs-Exportify.csv\nC:/path/your-songs-TuneMyMusic.csv"} value={form.playlistCsvPaths} onChange={onText("playlistCsvPaths")}></textarea>
          </label>

          <label>
            <span className="label-title">
              Prefetch CSV lyrics
              <span className="help-dot" tabIndex={0} data-tip="Loads tracks from CSV and caches lyrics from LRCLib before monitoring.">?</span>
            </span>
            <input type="checkbox" checked={form.prefetchCsvLyrics} onChange={onBool("prefetchCsvLyrics")} />
          </label>

          <label>
            <span className="label-title">
              Prefetch only
              <span className="help-dot" tabIndex={0} data-tip="Only build lyrics cache from CSV/API and exit without starting monitor.">?</span>
            </span>
            <input type="checkbox" checked={form.prefetchOnly} onChange={onBool("prefetchOnly")} />
          </label>

          <label className="wide">
            <span className="label-title">
              Hugging Face token (optional)
              <span className="help-dot" tabIndex={0} data-tip="Optional token used for faster model downloads and higher Hugging Face Hub rate limits.">?</span>
            </span>
            <input type="password" placeholder="hf_..." value={form.hfToken} onChange={onText("hfToken")} />
          </label>

          <label className="wide">
            <span className="label-title">
              Spotify access token (optional)
              <span className="help-dot" tabIndex={0} data-tip="Only needed for Spotify API features like currently-playing pre-duck and playlist API prefetch.">?</span>
            </span>
            <input type="password" placeholder="BQ..." value={form.spotifyToken} onChange={onText("spotifyToken")} disabled={form.csvExportMode} />
          </label>

          <label>
            <span className="label-title">
              Playlist ID (optional)
              <span className="help-dot" tabIndex={0} data-tip="Used with prefetch to cache lyrics for all tracks in a playlist.">?</span>
            </span>
            <input type="text" placeholder="37i9dQZF..." value={form.playlistId} onChange={onText("playlistId")} disabled={form.csvExportMode} />
          </label>

          <label>
            <span className="label-title">
              Prefetch playlist lyrics
              <span className="help-dot" tabIndex={0} data-tip="When enabled, the monitor preloads lyrics cache for the playlist ID at start.">?</span>
            </span>
            <input type="checkbox" checked={form.prefetchPlaylistLyrics} onChange={onBool("prefetchPlaylistLyrics")} disabled={form.csvExportMode} />
          </label>

          <label>
            <span className="label-title">
              Lyrics preduck seconds
              <span className="help-dot" tabIndex={0} data-tip="How early to duck before profane lyric timestamps.">?</span>
            </span>
            <input type="number" min="0" max="5" step="0.1" value={form.lyricsPreduckSeconds} onChange={onText("lyricsPreduckSeconds")} />
          </label>
        </section>

        <section className="actions">
          <button type="button" className="cta" onClick={startMonitor} disabled={running || !tauriReady}>Start Monitor</button>
          <button type="button" className="secondary" onClick={stopMonitor} disabled={!running || !tauriReady}>Stop Monitor</button>
        </section>

        <section className="console-wrap">
          <h2>Live log</h2>
          <pre ref={logRef} className="console">{logLines.join("\n")}</pre>
        </section>
      </main>
    </>
  );
}

export default App;
