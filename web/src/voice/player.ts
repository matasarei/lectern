import { useSyncExternalStore } from "react";

import { apiRaw } from "../api/client";

/** Where the sound goes: the part of an AudioContext the player uses. */
export interface AudioOut {
  readonly destination: AudioNode;
  resume(): Promise<void>;
  decodeAudioData(data: ArrayBuffer): Promise<AudioBuffer>;
  createBufferSource(): AudioBufferSourceNode;
}

/** One sentence spoken, as WAV bytes. */
export type FetchSpeech = (text: string, language: string, signal: AbortSignal) => Promise<ArrayBuffer>;

export type ReadingStatus = "loading" | "playing";

export interface Reading {
  key: string; // which reply
  status: ReadingStatus;
}

async function fetchSpeech(text: string, language: string, signal: AbortSignal): Promise<ArrayBuffer> {
  const response = await apiRaw("/api/voice/speak", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, language }),
    signal,
  });
  return response.arrayBuffer();
}

let context: AudioContext | null = null;

/** One AudioContext for the page, made on the first reading: a browser lets it play once the
 * person has clicked something, which reading aloud follows. */
function pageAudio(): AudioOut {
  context ??= new AudioContext();
  return context;
}

/** Reads replies aloud, one at a time for the whole page: starting one stops the other. Each
 * sentence is fetched while the one before it plays, so there's no gap to wait for. Sound goes
 * through Web Audio from the fetched bytes, which the page's CSP allows (no media URL). */
export class Player {
  private reading: Reading | null = null;
  private run: object | null = null; // the reading going on; a stopped one sees it changed
  private stopRun: (() => void) | null = null;
  private readonly listeners = new Set<() => void>();

  constructor(
    private readonly audio: () => AudioOut = pageAudio,
    private readonly fetch: FetchSpeech = fetchSpeech,
  ) {}

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  current = (): Reading | null => this.reading;

  /** Read the sentences in this language. Resolves when they've all been read or reading
   * stopped; rejects when a sentence couldn't be fetched or played. */
  async read(key: string, sentences: string[], language: string): Promise<void> {
    this.stop();
    const run = {};
    const controller = new AbortController();
    let source: AudioBufferSourceNode | null = null;
    let finish: () => void = () => {};
    this.run = run;
    this.stopRun = () => {
      controller.abort();
      source?.stop();
      finish();
    };
    this.set({ key, status: "loading" });
    try {
      const out = this.audio();
      await out.resume();
      const load = (i: number): Promise<AudioBuffer> =>
        this.fetch(sentences[i], language, controller.signal).then((bytes) => out.decodeAudioData(bytes));
      let next = sentences.length > 0 ? load(0) : null;
      for (let i = 0; next !== null; i++) {
        const buffer = await next;
        next = i + 1 < sentences.length ? load(i + 1) : null;
        next?.catch(() => {}); // a failure is seen when it is awaited, not as unhandled
        if (this.run !== run) return;
        this.set({ key, status: "playing" });
        await new Promise<void>((resolve) => {
          finish = resolve;
          source = out.createBufferSource();
          source.buffer = buffer;
          source.connect(out.destination);
          source.onended = () => resolve();
          source.start();
        });
        source = null;
        if (this.run !== run) return;
      }
    } catch (error) {
      if (this.run === run) throw error; // a stopped reading's abort isn't an error
    } finally {
      if (this.run === run) {
        this.run = null;
        this.stopRun = null;
        this.set(null);
      }
    }
  }

  stop(): void {
    const stop = this.stopRun;
    this.run = null;
    this.stopRun = null;
    stop?.();
    this.set(null);
  }

  private set(reading: Reading | null): void {
    if (reading?.key === this.reading?.key && reading?.status === this.reading?.status) return;
    this.reading = reading;
    for (const listener of this.listeners) listener();
  }
}

export const player = new Player();

/** How the reply `key` is being read now: "loading", "playing", or null when it isn't. */
export function useReading(key: string, from: Player = player): ReadingStatus | null {
  const reading = useSyncExternalStore(from.subscribe, from.current);
  return reading?.key === key ? reading.status : null;
}
