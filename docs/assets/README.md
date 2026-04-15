# Khala — branding assets

Drop generated branding assets in this folder.

## Expected files

| File | Used by | Size / aspect | How to regenerate |
|---|---|---|---|
| `khala-banner.png` | Top-level [`README.md`](../../README.md) hero | Ultrawide **21:9** (e.g. 1568×672) | See the Nano Banana prompt below |

## Nano Banana prompt for `khala-banner.png`

Paste this into [Nano Banana](https://nanobanana.ai/) / Gemini 2.5 Flash Image and select aspect ratio **21:9**:

> Epic cinematic banner, ultrawide 21:9, hyper-detailed concept art, hero shot. A colossal swirling vortex of electric-cyan and violet psionic energy dominates the center, crackling with lightning and shooting tendrils of gold light outward. Being pulled into this vortex from all sides: dozens of glowing holographic glyphs and icons representing specialist AI agents — a blueprint, a quill, a circuit board, a microscope, a stock chart, a compass, a chef's knife, a shield, a stethoscope, a gavel, a rocket, a gear — each one streaking with motion blur and leaving neon comet trails. Where the tendrils converge at the core, they fuse into a single blinding gold-white singularity that radiates lens flares and volumetric god-rays across the entire frame, pointing outward toward the viewer as if the entire storm is aimed at them. Obsidian-black background bleeding into deep purple nebula, shot through with electric-blue nerve-like filaments and scattered star bursts. Dramatic rim lighting, heavy atmospheric haze, chromatic aberration on the highlights, cinematic bloom. On the left third, the word "KHALA" rendered HUGE in a bold, sharp, futuristic display typeface — brilliant white core with a thick cyan outer glow and faint violet shadow, edges fizzing with tiny electric sparks. Directly beneath it, tagline in thin spaced uppercase sans-serif, slightly smaller and in warm gold: "MANY MINDS · ONE OBJECTIVE · YOURS". Style references: the marketing art of Nvidia GTC keynotes, Cyberpunk 2077 splash screens, Dune: Part Two posters, and Marvel title cards. Maximum drama, maximum contrast, unapologetically bold. No humans, no recognizable sci-fi IP, no text errors, no watermarks, no borders.

### Optional remixes

- Swap the tagline in the prompt: `"MANY MINDS · ONE OBJECTIVE · YOURS"` → `"THE HIVEMIND FOR BUILDERS"` or `"MANY MINDS · ONE STORM"`
- Add `"bright god-rays piercing through the logo letters"` for extra typography drama
- Add `"foreground debris and floating glass shards catching the light"` for Marvel-poster feel
- Swap `"cyan and violet"` → `"magenta and teal"` for a synthwave palette
- Append `"shot on 70mm IMAX"` to push photo-realism
- Append `"oil painting texture, thick impasto brushstrokes"` for a painterly hero

Save the final render as `khala-banner.png` in this folder and the top-level README will pick it up automatically.
