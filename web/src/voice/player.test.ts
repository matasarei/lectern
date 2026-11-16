import { describe, expect, it, vi } from "vitest";

import { type AudioOut, type FetchSpeech, Player } from "./player";

/** A sentence's "sound": its text, as the bytes and as the decoded buffer. */
type FakeBuffer = AudioBuffer & { text: string };

class FakeSource {
  buffer: FakeBuffer | null = null;
  onended: (() => void) | null = null;
  stopped = false;
  constructor(private readonly out: FakeOut) {}
  connect() {}
  start() {
    this.out.playing.push(this);
  }
  stop() {
    this.stopped = true;
  }
  /** The sentence finished playing. */
  end() {
    this.onended?.();
  }
}

class FakeOut implements AudioOut {
  readonly destination = {} as AudioNode;
  playing: FakeSource[] = [];
  resume = vi.fn(async () => {});
  async decodeAudioData(data: ArrayBuffer): Promise<AudioBuffer> {
    return { text: new TextDecoder().decode(data) } as FakeBuffer;
  }
  createBufferSource(): AudioBufferSourceNode {
    return new FakeSource(this) as unknown as AudioBufferSourceNode;
  }
  get texts(): string[] {
    return this.playing.map((s) => (s.buffer as FakeBuffer).text);
  }
}

/** A server the test answers: each sentence's request waits until the test lets it through. */
function fakeServer() {
  const asked: { text: string; language: string; signal: AbortSignal; answer: () => void; fail: (e: Error) => void }[] = [];
  const fetch: FetchSpeech = (text, language, signal) =>
    new Promise((resolve, reject) => {
      asked.push({
        text,
        language,
        signal,
        answer: () => resolve(new TextEncoder().encode(text).buffer as ArrayBuffer),
        fail: reject,
      });
      signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    });
  return { asked, fetch };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("Player", () => {
  it("plays the sentences in order, fetching the next while one plays", async () => {
    const out = new FakeOut();
    const server = fakeServer();
    const player = new Player(() => out, server.fetch);

    const reading = player.read("r1", ["One.", "Two.", "Three."], "en");
    await settle();
    expect(server.asked.map((a) => a.text)).toEqual(["One."]);
    expect(player.current()).toEqual({ key: "r1", status: "loading" });

    server.asked[0].answer();
    await settle();
    expect(out.texts).toEqual(["One."]);
    expect(player.current()).toEqual({ key: "r1", status: "playing" });
    expect(server.asked.map((a) => a.text)).toEqual(["One.", "Two."]); // fetched ahead
    expect(server.asked[1].language).toBe("en");

    server.asked[1].answer();
    out.playing[0].end();
    await settle();
    expect(out.texts).toEqual(["One.", "Two."]);

    server.asked[2].answer();
    out.playing[1].end();
    await settle();
    out.playing[2].end();
    await reading;
    expect(out.texts).toEqual(["One.", "Two.", "Three."]);
    expect(player.current()).toBeNull();
    expect(out.resume).toHaveBeenCalled();
  });

  it("stops at once: the sound, and the sentence being fetched", async () => {
    const out = new FakeOut();
    const server = fakeServer();
    const player = new Player(() => out, server.fetch);

    const reading = player.read("r1", ["One.", "Two."], "en");
    await settle();
    server.asked[0].answer();
    await settle();

    player.stop();
    await reading; // resolves: stopping isn't an error

    expect(out.playing[0].stopped).toBe(true);
    expect(server.asked[1].signal.aborted).toBe(true);
    expect(player.current()).toBeNull();
  });

  it("starting another reply stops the first, which leaves the new one alone", async () => {
    const out = new FakeOut();
    const server = fakeServer();
    const player = new Player(() => out, server.fetch);

    const first = player.read("r1", ["One."], "en");
    await settle();
    const second = player.read("r2", ["Раз."], "ru");
    await first;
    await settle();

    expect(server.asked[0].signal.aborted).toBe(true);
    expect(player.current()).toEqual({ key: "r2", status: "loading" });
    server.asked[1].answer();
    await settle();
    expect(out.texts).toEqual(["Раз."]);
    out.playing[0].end();
    await second;
    expect(player.current()).toBeNull();
  });

  it("rejects when a sentence can't be fetched, and stops reading", async () => {
    const out = new FakeOut();
    const server = fakeServer();
    const player = new Player(() => out, server.fetch);

    const reading = player.read("r1", ["One."], "en");
    await settle();
    server.asked[0].fail(new Error("409"));

    await expect(reading).rejects.toThrow("409");
    expect(player.current()).toBeNull();
    expect(out.playing).toEqual([]);
  });

  it("tells its listeners when what it reads changes", async () => {
    const out = new FakeOut();
    const server = fakeServer();
    const player = new Player(() => out, server.fetch);
    const heard: (string | null)[] = [];
    const unsubscribe = player.subscribe(() => heard.push(player.current()?.status ?? null));

    const reading = player.read("r1", ["One."], "en");
    await settle();
    server.asked[0].answer();
    await settle();
    out.playing[0].end();
    await reading;
    unsubscribe();

    expect(heard).toEqual(["loading", "playing", null]);
  });
});
