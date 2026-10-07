# Hoeffding's D: a franken_manim explainer

A 3Blue1Brown-style explainer of **Hoeffding's D**, about 7½ minutes long and narrated. It was written entirely against
[franken_manim](../franken_manim)'s native Rust front door, with no Python and no LaTeX.
The voice-over is spoken by [FrankenTTS](../frankentts) in the **robert** voice, and ffmpeg
is used only for the encode, the final join and loudness normalization.

**Final video:** `renders/final_4k_narrated/hoeffdings_d_explainer_4k.mp4`, at 3840×2160, 60 fps, H.264 High (NVENC p7, constant quality 16), with AAC narration normalized to −16 LUFS.
An earlier unnarrated 1080p cut is in `renders/final_1080p60/`.

## Chapters

| # | Chapter | What it shows | franken_manim features exercised |
|---|---|---|---|
| 1 | `01_hook` | A ring of 150 points, which Pearson, Spearman and Kendall all call "≈ 0", followed by the title card | `grow_arrow`, lagged `fade_in`, live `DecimalNumber` readouts, `show_passing_flash`, `indicate`, `write` |
| 2 | `02_gallery` | One cloud morphs through line, parabola, ring, X, wave and noise. Four measures update live, each judged against its own permutation-test noise ceiling | `Transform` of a 150-dot group, `always_redraw` bars, value-tracker tweens, `replacement_transform`, `SurroundingRectangle` |
| 3 | `03_quadruples` | 2 points show a direction, 3 a bend, 4 a turn; C(5000, 4) counts up | `FunctionGraph`, `grow_from_center`, `\binom` and `\frac` in native TeX, a comma-grouped counter |
| 4 | `04_ranks` | The article's worked example: heights and weights become ranks (ties average) in rank space, and an outlier doesn't matter | `transform_from_copy` cell-by-cell, tie highlighting, `.animate().move_to` |
| 5 | `05_counting` | Q_i as a lower-left quadrant count, compared with the independence baseline (R−1)(S−1)/(N−1) | `grow_from_point` quadrants, `Tex` with `t2c` by source identity |
| 6 | `06_formula` | D₁, D₂, D₃ counted up live, then Hoeffding's normalization, giving **D = 0.4107** | `TransformMatchingTex` over native span maps, live sums |
| 7 | `07_shuffle` | Shuffling Y keeps the marginals (rugs) and destroys the pairing. D is **recomputed from the moving dots every frame**, alongside a live 4×4 joint heat map; then a 2,000-shuffle permutation test | `always_redraw` closures that read live positions, `grow_from_edge` histogram |
| 8 | `08_outro` | A summary table, D's properties and credits | mini scatter glyphs, lagged reveals |

Every number on screen is computed by the scene. `stats.rs` is pinned at startup to the article's
worked example (R, S, Q, D₁ = 196.25, D₂ = 10696, D₃ = 1329.5, D = 0.410714…) and to scipy's
Pearson, Spearman and Kendall values. The data come from the engine's own seeded PCG64DXSM RNG.

## Narration: the picture follows the voice

- `src/narration.rs` holds the whole script: 61 lines, each keyed to the beat that triggers it.
- In a scene, `kit.say(stage, "g5")` first waits for the previous line to finish, then places the line's WAV at the current scene time through franken_manim's native, sample-exact mixer (`add_sound`, BN-14). `kit.hold(stage)` lets a line finish before the scene moves on.
- So animation timing adapts to the actual spoken lengths.
- Without `--narration`, the same scenes keep their visual-only pacing.

The background score (`sound.rs`) is a soft chord pad per chapter plus bell chimes on key reveals. It is synthesized in plain Rust and ducked under the voice.

```bash
./narrate.sh "$B" "$FTTS"             # ftts say --voice robert, one WAV per line (resumable)
python3 qa_narration.py "$B"          # franken_whisper transcribes every line and diffs it with the script
HOEFFDING_CUE_LOG=1 $B render ...     # logs each line's start time, for checking that picture and voice line up
```

The QA pass caught one line where "A cross" is acoustically "across"; the line was rewritten. All 61 lines now match the script.

## Build and render

```bash
# Uses ../franken_manim/crates/fmn (path dependency) and its pinned nightly.
RCH_SHIM_LOCAL_IDE=1 cargo build --release
B=$CARGO_TARGET_DIR/release/hoeffding       # or target/release/hoeffding

$B stats                                     # every number the video shows
$B script                                    # the narration as TSV
$B render all --res 960x540 --fps 30 --narration narration --out draft          # fast draft
$B render all --res 3840x2160 --fps 60 --crf 16 --preset slow \
              --narration narration --out renders/final_4k                      # delivery

cd renders/final_4k && ../../master.sh      # join chapters + two-pass loudnorm (video stream copied)
```

### Delivery quality

`fmn::render` sends libx264 no rate control (x264's CRF 23 default) and caps each ffmpeg job at
600 s (beads `fm-video-encode-quality-knob-fm85` and `fm-render-ffmpeg-job-limits-laeo`).

`src/encode.rs` therefore injects an `FfmpegCapability` whose process runner wraps the standard exact-image runner. It adds `-crf/-preset/-tune animation` to the libx264 job and `-b:a 256k` to the mux, and raises the timeout. Everything still runs through franken_manim's sandboxed boundary. The x264 SEI in the output confirms the settings.

On macOS, `render` also retries a chapter when the known transient Darwin `killpg` EPERM race fails it (bead `fm-darwin-killpg-eperm-race-7aae`). Publication is atomic, so nothing partial is left behind.

## How much you have to download

The whole video comes from one 9.2 MB binary. ffmpeg is the only other thing it needs, to write MP4. Making the same kind of video with legacy manim means installing a Python stack and a LaTeX distribution. These figures were measured on macOS arm64 in October 2026, and exclude the FrankenTTS narration.

| Stack | Download | Installed |
|---|---|---|
| **This explainer** (`hoeffding`, 9,242,880 B) + Homebrew ffmpeg | **58.1 MB** | **154.3 MB** |
| `fmn` CLI (13,030,096 B) + Homebrew ffmpeg | 61.9 MB | 158.1 MB |
| manim CE 0.21 + MacTeX 2026 (the docs' recommendation) | 7,001.8 MB | 10,875.4 MB |
| manim CE 0.21 + BasicTeX + manim's tlmgr extras (the smallest LaTeX that works) | 351.5 MB | 865.4 MB |
| manimgl 1.7.2 + MacTeX 2026 | 7,059.1 MB | 11,064.1 MB |
| manimgl 1.7.2 + BasicTeX + extras | 408.8 MB | 1,054.1 MB |
| Linux: manim CE + `texlive-full` via apt (the docs' Linux recommendation) | 4,679.3 MB | 9,472.4 MB |

- **Inside the binary.** `hoeffding` links only macOS system libraries: libSystem, libobjc, libiconv, and the IOKit, OpenDirectory, Foundation and CoreFoundation frameworks. Its TeX engine (fmd-math) and fonts (Computer Modern and IBM Plex Sans, about 3.7 MB) are compiled in.
- **Proof that nothing external is used.** I sandboxed the binary so it could not read MacTeX, Homebrew or any system font folder. It still typeset the formula chapter byte-identically, and with only ffmpeg allowed to run, it wrote a valid MP4.
- **Legacy manim rows** add up:
  - a uv-managed CPython 3.12 and the package's wheels;
  - Homebrew `cairo pkg-config`, plus `ffmpeg` for manimgl (CE bundles its own ffmpeg inside PyAV);
  - the LaTeX distribution: MacTeX at 6,865.0 MB download and 10,449.3 MB installed, according to the package's own install-size field.
- **Not counted.** pycairo compiles from source on macOS, so legacy manim also needs the Xcode Command Line Tools: 1,958.5 MB on this Mac, not counted above.

## The second front door

`portal/hoeffding_portal.py` holds two segments (the ring hook, and the live-D shuffle) written as plain `manimlib` scene code. They render through the separately installed `fmn-python` portal:

```bash
fmn-python portal/hoeffding_portal.py RingHook ShuffleLiveD --format mp4 \
    --resolution 1920x1080 --fps 60 --video_dir out.mp4
```

## Known engine limitations worked around

- `\rho` and `\theta` in math mode render as ϱ and ϑ (bead `fm-5wq.60`), so Spearman's ρ uses the text face's upright ρ.
- Native `Axes` cannot be repositioned with `c2p` following (bead `fm-native-axes-reposition-n1yd`), so the scenes use a small `Frame2` data-window helper (`kit.rs`).
- There is no 2D camera pan or zoom on the fast retained route, so the scenes have none.
- Frames render serially (bead `fm-sq8.5`), so a 4K render uses about 1–2 cores of 14. The final cut took roughly an hour.
