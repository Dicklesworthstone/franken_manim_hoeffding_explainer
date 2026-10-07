//! Hoeffding's D — a 3Blue1Brown-style explainer, rendered end to end by
//! franken_manim's native Rust front door (no Python, no LaTeX) and encoded
//! through its one sandboxed external tool, ffmpeg.
//!
//! ```text
//! hoeffding stats                      # the numbers every chapter shows
//! hoeffding glyphs OUT.png             # typesetting probe sheet
//! hoeffding script                     # narration lines as TSV (id, text)
//! hoeffding render all|<chapter>... [--res 1920x1080] [--fps 60] [--out DIR]
//!                  [--narration DIR] [--crf N] [--preset P] [--silent]
//!                  [--encoder libx264|h264_nvenc|hevc_nvenc] [--gpu N] [--threads N]
//! ```

mod chapters;
mod encode;
mod kit;
mod narration;
mod sound;
mod stats;

use std::path::PathBuf;
use std::time::Instant;

use fmn::prelude::*;

use crate::kit::{BACKGROUND, Kit};

struct Args {
    command: String,
    targets: Vec<String>,
    res: (u32, u32),
    fps: u32,
    out: PathBuf,
    silent: bool,
    narration: Option<PathBuf>,
    crf: Option<u8>,
    preset: &'static str,
    threads: Option<u32>,
    encoder: String,
    gpu: Option<u32>,
    max_frames: Option<u64>,
}

fn parse_args() -> Result<Args, String> {
    let mut it = std::env::args().skip(1);
    let command = it.next().ok_or("usage: hoeffding stats | glyphs OUT.png | render all|CHAPTER... [--res WxH] [--fps N] [--out DIR]")?;
    let mut args = Args {
        command,
        targets: Vec::new(),
        res: (1920, 1080),
        fps: 60,
        out: PathBuf::from("renders"),
        silent: false,
        narration: None,
        crf: None,
        preset: "medium",
        threads: None,
        encoder: "libx264".to_owned(),
        gpu: None,
        max_frames: None,
    };
    while let Some(a) = it.next() {
        match a.as_str() {
            "--res" => {
                let r = it.next().ok_or("--res needs WxH")?;
                let (w, h) = r.split_once('x').ok_or("--res needs WxH")?;
                args.res = (
                    w.parse().map_err(|_| "bad width")?,
                    h.parse().map_err(|_| "bad height")?,
                );
            }
            "--fps" => {
                args.fps = it
                    .next()
                    .ok_or("--fps needs N")?
                    .parse()
                    .map_err(|_| "bad fps")?
            }
            "--out" => args.out = PathBuf::from(it.next().ok_or("--out needs DIR")?),
            "--silent" => args.silent = true,
            "--narration" => {
                args.narration = Some(PathBuf::from(it.next().ok_or("--narration needs DIR")?))
            }
            "--encoder" => {
                let e = it.next().ok_or("--encoder needs a name")?;
                if !matches!(
                    e.as_str(),
                    "libx264" | "h264_nvenc" | "hevc_nvenc" | "h264_videotoolbox"
                ) {
                    return Err(
                        "--encoder is libx264, h264_nvenc, hevc_nvenc or h264_videotoolbox".into(),
                    );
                }
                args.encoder = e;
            }
            "--max-frames" => {
                args.max_frames = Some(
                    it.next()
                        .ok_or("--max-frames needs N")?
                        .parse()
                        .map_err(|_| "bad frame count")?,
                )
            }
            "--gpu" => {
                args.gpu = Some(
                    it.next()
                        .ok_or("--gpu needs N")?
                        .parse()
                        .map_err(|_| "bad gpu index")?,
                )
            }
            "--threads" => {
                args.threads = Some(
                    it.next()
                        .ok_or("--threads needs N")?
                        .parse()
                        .map_err(|_| "bad thread count")?,
                )
            }
            "--crf" => {
                args.crf = Some(
                    it.next()
                        .ok_or("--crf needs N")?
                        .parse()
                        .map_err(|_| "bad crf")?,
                )
            }
            "--preset" => {
                args.preset = match it.next().ok_or("--preset needs a name")?.as_str() {
                    "ultrafast" => "ultrafast",
                    "veryfast" => "veryfast",
                    "faster" => "faster",
                    "fast" => "fast",
                    "medium" => "medium",
                    "slow" => "slow",
                    "slower" => "slower",
                    "veryslow" => "veryslow",
                    _ => return Err("unknown x264 preset".into()),
                }
            }
            other => args.targets.push(other.to_owned()),
        }
    }
    Ok(args)
}

fn options(
    path: PathBuf,
    format: RenderFormat,
    res: (u32, u32),
    fps: u32,
) -> Result<RenderOptions, Box<dyn std::error::Error>> {
    let mut options = RenderOptions::with_format(path, format)?;
    options.config.camera.resolution = res;
    options.config.camera.fps = fps;
    options.config.camera.background_color = BACKGROUND.to_owned();
    Ok(options)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    stats::self_check();
    let args = parse_args()?;
    match args.command.as_str() {
        "stats" => print_stats(),
        "script" => {
            for (id, text) in narration::SCRIPT {
                println!("{id}\t{text}");
            }
        }
        "glyphs" => {
            let out = args
                .targets
                .first()
                .cloned()
                .unwrap_or_else(|| "glyphs.png".into());
            let mut scene = chapters::glyphs::Glyphs { kit: Kit::new()? };
            let report = render(
                &mut scene,
                options(out.into(), RenderFormat::PngSequence, args.res, 1)?,
            )?;
            println!("glyph sheet -> {}", report.artifact.path.display());
        }
        "render" => {
            let all = chapters::registry();
            let wanted: Vec<&chapters::Entry> =
                if args.targets.iter().any(|t| t == "all") || args.targets.is_empty() {
                    all.iter().collect()
                } else {
                    args.targets
                        .iter()
                        .map(|t| {
                            all.iter()
                                .find(|e| e.key == t)
                                .ok_or_else(|| format!("unknown chapter {t}"))
                        })
                        .collect::<Result<_, _>>()?
                };
            std::fs::create_dir_all(&args.out)?;
            let audio = args.out.join("audio");
            std::fs::create_dir_all(&audio)?;
            let narrator = match &args.narration {
                Some(dir) => {
                    let n = narration::Narrator::load(dir)?;
                    println!(
                        "narration: {} lines, {:.1}s of speech from {}",
                        narration::SCRIPT.len(),
                        n.total_seconds(),
                        dir.display()
                    );
                    Some(n)
                }
                None => None,
            };
            let total = Instant::now();
            let mut frames = 0;
            for entry in wanted {
                let path = args.out.join(format!("{}.mp4", entry.key));
                let started = Instant::now();
                // macOS: a transient Darwin killpg EPERM after ffmpeg exits fails
                // the whole render (fm-darwin-killpg-eperm-race-7aae). Publication
                // is atomic, so nothing partial exists; render the chapter again.
                let mut attempt = 1;
                let report = loop {
                    let mut kit = Kit::new()?;
                    if !args.silent {
                        kit.score = Some(sound::Score {
                            dir: audio.clone(),
                            under_voice: narrator.is_some(),
                        });
                        if let Some(n) = &narrator {
                            n.reset();
                        }
                        kit.narrator = narrator.clone();
                    }
                    let mut scene = (entry.make)(kit);
                    let mut opts = options(path.clone(), RenderFormat::Mp4, args.res, args.fps)?;
                    if let Some(n) = args.threads {
                        opts.config.render.threads = fmn::config::config::ThreadPolicy::Fixed(n);
                    }
                    opts.config.file_writer.video_codec = args.encoder.clone();
                    if let Some(n) = args.max_frames {
                        // Profiling scenarios: stop after n frames (the render
                        // then fails by design and publishes nothing).
                        opts.max_frames = n;
                    }
                    if let Some(crf) = args.crf {
                        opts.ffmpeg = Some(encode::capability(encode::Quality {
                            quality: crf,
                            gpu: args.gpu,
                            preset: args.preset,
                            audio_bitrate: "256k",
                            timeout: std::time::Duration::from_secs(4 * 3600),
                        }));
                    }
                    match render(scene.as_mut(), opts) {
                        Ok(report) => break report,
                        Err(error)
                            if attempt < 3
                                && format!("{error:?}").contains(
                                    "process-tree completion kill failed: Operation not permitted",
                                ) =>
                        {
                            eprintln!(
                                "{}: transient Darwin EPERM after ffmpeg exit (attempt {attempt}); rendering again",
                                entry.key
                            );
                            attempt += 1;
                        }
                        Err(error) => return Err(error.into()),
                    }
                };
                let secs = started.elapsed().as_secs_f64();
                frames += report.artifact.frame_count;
                println!(
                    "{:<14} {:>5} frames  {:>6.1}s  {:>6.1} fps  -> {}{}",
                    entry.key,
                    report.artifact.frame_count,
                    secs,
                    report.artifact.frame_count as f64 / secs,
                    report.artifact.path.display(),
                    if attempt > 1 {
                        format!("  (attempt {attempt})")
                    } else {
                        String::new()
                    }
                );
            }
            let secs = total.elapsed().as_secs_f64();
            println!(
                "total          {frames:>5} frames  {secs:>6.1}s  {:>6.1} fps",
                frames as f64 / secs
            );
        }
        other => return Err(format!("unknown command {other}").into()),
    }
    Ok(())
}

fn print_stats() {
    use stats::Shape::*;
    for shape in [Line, Parabola, Ring, Cross, Wave, Noise] {
        let pts = stats::shape_points(shape, chapters::gallery::N, chapters::gallery::seed(shape));
        let m = stats::measures(&pts);
        let q = stats::null_q99(&pts, chapters::gallery::TRIALS, 99);
        println!(
            "{:10} r={:+.3} (q99 {:.3})  rho={:+.3} (q99 {:.3})  tau={:+.3} (q99 {:.3})  D={:+.4} (q99 {:.4})",
            shape.label(),
            m.pearson,
            q.pearson,
            m.spearman,
            q.spearman,
            m.kendall,
            q.kendall,
            m.hoeffding,
            q.hoeffding
        );
    }
    let h = stats::hoeffding(&stats::HEIGHTS, &stats::WEIGHTS);
    println!(
        "worked example: R={:?} S={:?} Q={:?} D1={} D2={} D3={} D={}",
        h.r, h.s, h.q, h.d1, h.d2, h.d3, h.d
    );
    println!("C(5000,4) = {}", stats::choose(5000, 4));
}
