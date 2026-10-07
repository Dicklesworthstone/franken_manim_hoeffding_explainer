//! Delivery-quality encoding through franken_manim's own ffmpeg boundary.
//!
//! `fmn::render` sends the encoder no quality settings (x264's default CRF
//! 23, preset medium) and caps each ffmpeg job at 600 s. The facade's
//! documented seam for hosts that govern processes themselves is an
//! injected `FfmpegCapability`; this runner wraps the standard exact-image
//! runner and only adjusts the argv/timeout of the jobs it starts.
//!
//! Two encoder families:
//! - `libx264`: constant rate factor, preset, `tune animation`.
//! - `h264_nvenc` / `hevc_nvenc`: NVENC constant quality (`-rc vbr -cq N
//!   -b:v 0`) at preset p7/hq with spatial+temporal AQ and B-frames as
//!   references. x264's slow-preset lookahead is largely serial, so a 4K
//!   encode tops out near one frame per second per stream on a Zen 3 core;
//!   NVENC removes the encoder from the critical path.

use std::sync::Arc;
use std::time::Duration;

use fmn::platform::process::{
    ProcessCancellation, ProcessError, ProcessMechanism, ProcessOutcome, ProcessRunner,
    ProcessSpec, ProcessStdinLimits, RunningProcess, StdFfmpegLocator, StdProcessRunner,
};
use fmn::prelude::FfmpegCapability;

pub struct Quality {
    /// x264 CRF, or NVENC constant-quality level.
    pub quality: u8,
    pub preset: &'static str,
    pub audio_bitrate: &'static str,
    pub timeout: Duration,
    /// NVENC device index.
    pub gpu: Option<u32>,
}

struct QualityRunner {
    inner: StdProcessRunner,
    quality: Quality,
}

impl QualityRunner {
    fn video_args(&self, encoder: &str) -> Vec<String> {
        let q = self.quality.quality.to_string();
        let mut args: Vec<String> = match encoder {
            "libx264" => vec![
                "-crf",
                &q,
                "-preset",
                self.quality.preset,
                "-tune",
                "animation",
            ]
            .into_iter()
            .map(str::to_owned)
            .collect(),
            "h264_nvenc" | "hevc_nvenc" => [
                "-preset",
                "p7",
                "-tune",
                "hq",
                "-rc",
                "vbr",
                "-cq",
                &q,
                "-b:v",
                "0",
                "-spatial_aq",
                "1",
                "-temporal_aq",
                "1",
                "-rc-lookahead",
                "32",
                "-bf",
                "3",
                "-b_ref_mode",
                "middle",
            ]
            .into_iter()
            .map(str::to_owned)
            .collect(),
            _ => Vec::new(),
        };
        if encoder.ends_with("_nvenc")
            && let Some(gpu) = self.quality.gpu
        {
            args.extend(["-gpu".to_owned(), gpu.to_string()]);
        }
        args
    }

    fn adjust(&self, spec: &ProcessSpec) -> ProcessSpec {
        let mut s = spec.clone();
        // The video encode: insert rate control right after `-c:v <encoder>`.
        if let Some(i) = s
            .argv
            .windows(2)
            .position(|w| w[0] == "-c:v" && w[1] != "copy")
            && !s.argv.iter().any(|a| a == "-crf" || a == "-cq")
        {
            let extra = self.video_args(&s.argv[i + 1].clone());
            s.argv.splice(i + 2..i + 2, extra);
        }
        // The audio mux: a transparent AAC bitrate for the narration.
        if let Some(i) = s
            .argv
            .windows(2)
            .position(|w| w[0] == "-c:a" && w[1] == "aac")
            && !s.argv.iter().any(|a| a == "-b:a")
        {
            s.argv.splice(
                i + 2..i + 2,
                ["-b:a".to_owned(), self.quality.audio_bitrate.to_owned()],
            );
        }
        s.timeout = s.timeout.max(self.quality.timeout);
        s
    }
}

impl ProcessRunner for QualityRunner {
    fn mechanism(&self) -> ProcessMechanism {
        self.inner.mechanism()
    }

    fn start(
        &self,
        spec: &ProcessSpec,
        cancellation: ProcessCancellation,
        stdin_limits: ProcessStdinLimits,
    ) -> Result<Box<dyn RunningProcess>, ProcessError> {
        self.inner
            .start(&self.adjust(spec), cancellation, stdin_limits)
    }

    fn run(&self, spec: &ProcessSpec) -> Result<ProcessOutcome, ProcessError> {
        self.inner.run(&self.adjust(spec))
    }
}

/// The host ffmpeg capability with delivery-quality rate control.
pub fn capability(quality: Quality) -> FfmpegCapability {
    FfmpegCapability {
        runner: Arc::new(QualityRunner {
            inner: StdProcessRunner,
            quality,
        }),
        locator: Arc::new(StdFfmpegLocator::from_host_path()),
        workdir_root: std::env::temp_dir(),
    }
}
