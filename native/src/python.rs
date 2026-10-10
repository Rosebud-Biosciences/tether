//! The `tether_native` Python module. `tether.backends.file` imports it when
//! it is installed and speaks `API_VERSION`; nothing else should.

use std::collections::HashMap;
use std::io;
use std::path::PathBuf;

use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;

use crate::listing;
use crate::scan::{self, ScanError};

/// Bumped whenever a signature below changes; tether uses the module only when
/// it expects this number.
pub const API_VERSION: u32 = 1;

fn to_py(error: ScanError) -> PyErr {
    match error {
        // `OSError(errno, strerror, filename)` picks the subclass from the
        // errno, so a PermissionError reads exactly as Python's own.
        ScanError::Io { error, path } => match error.raw_os_error() {
            Some(code) => {
                let text = io::Error::from_raw_os_error(code).to_string();
                let suffix = format!(" (os error {code})");
                let strerror = text.strip_suffix(&suffix).unwrap_or(&text).to_owned();
                // An `OsString` arrives as `str`, as Python's own filenames do.
                PyOSError::new_err((code, strerror, path.into_os_string()))
            }
            None => PyOSError::new_err(format!("{}: {error}", path.display())),
        },
        ScanError::NotUtf8 { path } => {
            PyValueError::new_err(format!("not UTF-8: {}", path.display()))
        }
    }
}

/// The hash cache of one `file` backend, and the scans that use it.
#[pyclass(frozen, module = "tether_native")]
struct HashCache {
    inner: scan::Cache,
}

#[pymethods]
impl HashCache {
    #[new]
    #[pyo3(signature = (path=None))]
    fn new(path: Option<PathBuf>) -> Self {
        HashCache {
            inner: scan::Cache::new(path),
        }
    }

    /// Files read since construction.
    #[getter]
    fn hashed(&self) -> u64 {
        self.inner.hashed()
    }

    /// `(count, size, digest, listing)` of the directory `root`.
    fn fingerprint_dir(
        &self,
        py: Python<'_>,
        root: PathBuf,
        owner_marker: &str,
        racy_window_ns: i64,
        workers: usize,
    ) -> PyResult<(usize, u64, String, Listing)> {
        let owner_marker = owner_marker.to_owned();
        let (count, size, digest, inner) = py
            .detach(|| {
                self.inner
                    .fingerprint_dir(&root, &owner_marker, racy_window_ns, workers)
            })
            .map_err(to_py)?;
        Ok((count, size, digest, Listing { inner }))
    }

    /// `(size, sha256)` of the file `path`.
    fn fingerprint_file(
        &self,
        py: Python<'_>,
        path: PathBuf,
        racy_window_ns: i64,
    ) -> PyResult<(u64, String)> {
        py.detach(|| self.inner.fingerprint_file(&path, racy_window_ns))
            .map_err(to_py)
    }
}

/// A directory's rows, held here until a commit asks for their text.
#[pyclass(frozen, module = "tether_native")]
struct Listing {
    inner: listing::Listing,
}

#[pymethods]
impl Listing {
    /// The JSON lines a commit stores.
    fn text(&self, py: Python<'_>) -> String {
        py.detach(|| self.inner.text())
    }

    fn __len__(&self) -> usize {
        self.inner.len()
    }
}

/// `_dump_listing`: rows, path -> (token, size), as JSON lines in path order.
#[pyfunction]
fn dump_listing(rows: HashMap<String, (String, u64)>) -> String {
    let mut rows: Vec<_> = rows.into_iter().collect();
    rows.sort_unstable_by(|a, b| a.0.cmp(&b.0));
    let mut out = String::with_capacity(rows.len() * 120);
    for (path, (token, size)) in &rows {
        listing::push_row(&mut out, path, token, *size);
    }
    out
}

/// `(added, removed, modified, entries, truncated)` between two stored
/// listings, `entries` holding the first `cap` changes as `(path, change,
/// bytes)`. Raises ValueError on a listing the Python reader must judge.
#[pyfunction]
#[allow(clippy::type_complexity)]
fn diff_listings(
    py: Python<'_>,
    a: &str,
    b: &str,
    cap: usize,
) -> PyResult<(u64, u64, u64, Vec<(String, &'static str, i64)>, bool)> {
    let (a, b) = (a.to_owned(), b.to_owned());
    let diff = py
        .detach(|| listing::diff(&a, &b, cap))
        .map_err(PyValueError::new_err)?;
    let entries = diff
        .entries
        .into_iter()
        .map(|c| (c.path, c.change, c.bytes))
        .collect();
    Ok((
        diff.added,
        diff.removed,
        diff.modified,
        entries,
        diff.truncated,
    ))
}

#[pymodule]
mod tether_native {
    use pyo3::prelude::*;

    #[pymodule_export]
    use super::{diff_listings, dump_listing, HashCache, Listing};

    #[pymodule_init]
    fn init(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add("API_VERSION", super::API_VERSION)
    }
}
