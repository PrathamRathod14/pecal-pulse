"use client";
// Voice without LiveKit: the browser's own speech recognition (Edge/Chrome)
// and speech synthesis. Used automatically when LiveKit is not configured.
// Transcripts go through the same agent/tool stream as typed and LiveKit turns.
import { useEffect, useRef, useState } from "react";
import { useAui, useAuiState } from "@assistant-ui/react";
import { Mic, Square, Volume2 } from "lucide-react";
import { useSalesStore } from "@/modules/sales/store";

type Recognition = {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  maxAlternatives: number;
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
  abort: () => void;
};

function recognitionCtor(): (new () => Recognition) | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as Record<string, unknown>;
  return (w.SpeechRecognition || w.webkitSpeechRecognition || null) as (new () => Recognition) | null;
}

function plain(text: string) {
  return text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/[*_#>`|]/g, "")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 600);
}

const ERRORS: Record<string, string> = {
  "not-allowed": "Microphone permission was denied. Allow it in the browser's address bar and try again.",
  "service-not-allowed": "This browser blocked speech recognition. Use Microsoft Edge or Google Chrome.",
  "no-speech": "No speech was detected. Try again and speak after the orb turns active.",
  "audio-capture": "No microphone was found.",
  network: "The browser's speech service is unreachable (it needs an internet connection).",
};

export function BrowserVoicePanel() {
  const aui = useAui();
  const running = useAuiState((s) => s.thread.isRunning);
  const language = useSalesStore((s) => s.assistantLanguage);
  const chatReady = useSalesStore((s) => s.apiStatus === "connected" && !!s.chatStatus?.configured && !s.chatLoading);
  const response = useSalesStore((s) => s.voiceResponse);
  const set = useSalesStore((s) => s.set);
  const [state, setState] = useState<"idle" | "listening" | "speaking">("idle");
  const [error, setError] = useState("");
  const [interim, setInterim] = useState("");
  const rec = useRef<Recognition | null>(null);
  const finalText = useRef("");
  const seen = useRef<string | null>(null);
  const german = language === "de";
  const Ctor = recognitionCtor();
  const canSpeak = typeof window !== "undefined" && "speechSynthesis" in window;

  const speak = (text: string) => {
    if (!canSpeak || !text) return;
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(plain(text));
    u.lang = german ? "de-DE" : "en-US";
    u.onstart = () => setState("speaking");
    u.onend = () => setState((s) => (s === "speaking" ? "idle" : s));
    u.onerror = () => setState((s) => (s === "speaking" ? "idle" : s));
    window.speechSynthesis.speak(u);
  };

  // Speak the final reply of a spoken turn (same signal the LiveKit panel uses).
  useEffect(() => {
    if (!response || seen.current === response.id || response.kind === "progress") return;
    seen.current = response.id;
    speak(response.text);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [response]);

  useEffect(() => () => {
    rec.current?.abort();
    if (canSpeak) window.speechSynthesis.cancel();
  }, [canSpeak]);

  const start = () => {
    if (!Ctor) return;
    setError("");
    setInterim("");
    finalText.current = "";
    if (canSpeak) window.speechSynthesis.cancel();
    if (running) aui.thread.cancelRun();
    const r = new Ctor();
    r.lang = german ? "de-DE" : "en-US";
    r.interimResults = true;
    r.continuous = false;
    r.maxAlternatives = 1;
    r.onresult = (e) => {
      let text = "";
      for (let i = 0; i < e.results.length; i++) text += e.results[i][0].transcript;
      setInterim(text);
      if (e.results[e.results.length - 1].isFinal) finalText.current = text;
    };
    r.onerror = (e) => {
      if (e.error !== "aborted") setError(ERRORS[e.error] ?? `Speech recognition failed (${e.error}).`);
    };
    r.onend = async () => {
      rec.current = null;
      setState("idle");
      set({ voiceBusy: false });
      const text = (finalText.current || "").trim();
      setInterim("");
      if (!text) return;
      set({ voiceInputPending: true });
      await aui.thread.append({ role: "user", content: [{ type: "text", text }] });
    };
    rec.current = r;
    set({ voiceBusy: true });
    setState("listening");
    try {
      r.start();
    } catch {
      setState("idle");
      set({ voiceBusy: false });
      setError("Could not start the microphone.");
    }
  };

  const stop = () => {
    if (state === "listening") rec.current?.stop();
    else if (state === "speaking" && canSpeak) {
      window.speechSynthesis.cancel();
      setState("idle");
    }
  };

  if (!Ctor) {
    return (
      <div className="voice-panel browser-voice">
        <p className="voice-error" role="status">
          Voice needs LiveKit (set LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET in the root .env) or a browser with
          built-in speech recognition such as Microsoft Edge or Google Chrome. You can type below.
        </p>
      </div>
    );
  }

  const disabled = !chatReady;
  return (
    <div className="voice-panel browser-voice">
      <button
        className={`browser-voice-orb ${state}`}
        onClick={state === "idle" ? start : stop}
        disabled={disabled && state === "idle"}
        aria-pressed={state === "listening"}
        aria-label={state === "listening" ? "Stop listening and send" : state === "speaking" ? "Stop speaking" : "Start speaking"}
        title={disabled ? "Chat needs a model key first (GEMINI_API_KEY)" : undefined}
      >
        {state === "idle" ? <Mic size={26} /> : <Square size={20} />}
      </button>
      <div className="browser-voice-text">
        <select
          value={language}
          onChange={(e) => set({ assistantLanguage: e.target.value as "en" | "de" })}
          aria-label="Voice language"
          disabled={state !== "idle"}
        >
          <option value="en">EN · English</option>
          <option value="de">DE · Deutsch</option>
        </select>
        <span className="voice-status" role="status">
          {state === "listening"
            ? interim || (german ? "Hört zu … zum Senden tippen" : "Listening… tap to send")
            : state === "speaking"
              ? german ? "Spricht · zum Stoppen tippen" : "Speaking · tap to stop"
              : running
                ? german ? "Wird bearbeitet…" : "Working…"
                : german ? "Zum Sprechen tippen" : "Tap the mic to speak"}
        </span>
        <span className="browser-voice-note">Browser voice (LiveKit not configured)</span>
      </div>
      <button
        className="icon-button"
        aria-label="Test speaker"
        title="Test speaker"
        disabled={!canSpeak || state !== "idle"}
        onClick={() => speak(german ? "SignalPilot ist bereit." : "SignalPilot is ready.")}
      >
        <Volume2 size={17} />
      </button>
      {error && <p className="voice-error" role="alert">{error}</p>}
    </div>
  );
}
