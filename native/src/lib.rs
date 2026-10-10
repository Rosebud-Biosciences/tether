//! The native directory scanner behind tether-vcs's `file` backend: the same
//! walk, hash cache, digest and listings as `tether.backends.file`, without
//! the per-file interpreter work that dominates a large tree there.

#[cfg(not(unix))]
compile_error!("tether-native supports Unix only; tether uses its Python scanner elsewhere");

pub mod listing;
pub mod scan;

#[cfg(feature = "python")]
mod python;
