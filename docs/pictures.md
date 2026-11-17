# Making pictures

Lectern makes pictures on your own computer, with an image model on your own GPU. Describe
the picture in a chat, or give it pictures of your own to work from: a logo, a photo of a
thing to keep, a style to copy. Nothing is sent anywhere, and nothing runs until you've seen
the prompt and allowed it.

![A chat asking for a purple elephant with the logo from an attached file, the card that
allowed the picture, and the reply](assets/screenshots/pictures-chat.png)

## Asking for a picture

There are two ways to ask.

- **Just ask in the chat**, as you'd ask anyone: "Draw a purple elephant with the logo from
  the reference file". The chat writes the prompt and asks you before anything runs.
- **Switch on Image** beside the message box (the picture frame with a sparkle) and pick a
  shape. Your message is then always a request for a picture: the chat doesn't decide
  whether to make one.

You don't need to write the prompt the way image models like it. Before you're asked, Lectern
rewrites it the way the image model wants: one paragraph, the main subject first, then the
style, the setting, the light and the colours. The words you asked for in quotes stay as
they are, and it's written in English whatever language you wrote in.

## Working from your own pictures

To make a picture from a picture you have:

1. Get it into the project: **Attach** it to your message (the paper clip), drop it on the
   Overview, or use **Import files…**.
2. Attach it to your message with **@** if it's already in the project.
3. Say what to take from it and what to change: "the logo from the reference file on a
   purple elephant", "this cat, as a watercolour", "the same layout, at night".

The pictures attached to your message are the ones the image model gets. Up to 4 can be used
at once: PNG, JPEG, WebP, GIF, BMP or TIFF. What Lectern gives the model is a copy it makes
itself, upright and without the original's hidden details (such as where a photo was taken),
so your file is never changed.

A second model, one that can see pictures, looks at them first and writes the prompt by
their numbers: "holding the PHP logo from image 1". That's how the prompt can say what's in
your picture when you didn't.

**Which image model you use matters here.** FLUX.2 klein, the one Lectern picks for every
computer, takes your pictures themselves, so the result keeps what's in them. Z-Image-Turbo,
if you choose it under **Models & memory**, can't: your pictures are only described in words
in its prompt, which keeps far less of them. Its card says so.

## The card: what you approve

Before anything runs, a card in the chat asks **Make a picture with FLUX.2 klein 4B?** (or
whichever image model you use). It shows:

- **What the picture shows:** the rewritten prompt. Change anything you like before you
  run it.
- **Pictures to work from:** a thumbnail of each of your pictures. Untick one to leave it
  out. If you do, check the prompt doesn't still name it.
- **Shape:** square, portrait, landscape or a large square, each with its size and how long
  it should take on this computer.
- Anything that has to download first.

Choose **Run** to make it, or **Cancel** to make nothing. Every picture is asked about:
there's no "always run", because on a small GPU a picture can take minutes and unloads the
chat's model while it runs. Once decided, the card says **Ran** or **Cancelled**.
If you press Stop, or Lectern shuts down (an update, say), before you answer, nothing is
made and the card stays in the chat saying so.

To see exactly what the chat asked for, click the grey line above the card. It unfolds to the
whole call. That's the chat's own request: the card shows the prompt after it was rewritten.

![The image.generate line unfolded, showing the chat's prompt and the file
name](assets/screenshots/pictures-chat-call.png)

## Where the picture goes

The picture is saved to the project's `images/` folder and opens in a tab of its own. Its
details show where it came from, with a link back to the chat that made it. **Download**
saves a copy to your computer.

![The new picture open in a tab, with its details on the
right](assets/screenshots/pictures-result.jpg)

## The first time

The first picture downloads what it needs, and the card lists it before you run it:

- the image program, stable-diffusion.cpp, built for your computer;
- the image model. FLUX.2 klein 4B is about 5 GB on smaller GPUs and 7 GB on larger ones,
  which get the 8-bit version and larger pictures.
- for your own pictures, the model that looks at them: Qwen3-VL, about 3 GB (4B) or 6 GB
  (8B), depending on your computer.

## How long it takes

The card says how long each shape should take. It learns from this computer's own pictures,
so it gets closer after the first one. On a Mac with an M4 Max, a 1024 × 1024 picture took
about half a minute, and about a minute with one picture of your own to work from. A 4 GB
GPU makes smaller pictures by default and takes longer.

On a Mac, making pictures needs macOS 26 or later.

## Good to know

- **Say what should be there,** not what shouldn't: FLUX.2 ignores "no" and "without".
- **Put words you want in the picture in quotes,** and say where they go.
- **A picture the model already knows**, like a famous logo, comes out recognisable even
  without your file. To check your own picture is being used, look for its thumbnail on the
  card.
- **The chat can't see the picture it made.** It tells you it's done; open the tab to see
  it.

How it works inside, for anyone changing it: [The chat → Making pictures](agent.md#making-pictures)
and [Models and memory → Image models](models.md#image-models).
