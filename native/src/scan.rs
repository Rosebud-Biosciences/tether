//! Walk a local directory and hash what the cache cannot vouch for: the
//! `file` backend's `_walk` and `_HashCache`, with the same rules and the same
//! cache file, so either implementation can pick up where the other left off.

use std::cell::RefCell;
use std::collections::HashMap;
use std::ffi::OsStr;
use std::fs::{self, File, FileType};
use std::io::{self, BufWriter, Read, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Mutex, MutexGuard};
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::de::{self, Deserializer, SeqAccess, Visitor};
use serde::ser::{SerializeTuple, Serializer};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::listing::{Listing, Row, Token};

#[derive(Debug)]
pub enum ScanError {
    /// An OS error on `path`, reported as Python's `OSError` would be.
    Io { error: io::Error, path: PathBuf },
    /// A name that is not UTF-8: the digest and the listing need text.
    NotUtf8 { path: PathBuf },
}

fn io_error(error: io::Error, path: &Path) -> ScanError {
    ScanError::Io {
        error,
        path: path.to_path_buf(),
    }
}

fn utf8(path: &Path) -> Result<&str, ScanError> {
    path.to_str().ok_or_else(|| ScanError::NotUtf8 {
        path: path.to_path_buf(),
    })
}

/// The `os.stat` fields a cache entry is valid for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Stat {
    size: u64,
    mtime_ns: i64,
    ctime_ns: i64,
    ino: u64,
}

impl Stat {
    fn of(meta: &fs::Metadata) -> Self {
        Stat {
            size: meta.size(),
            mtime_ns: meta.mtime() * 1_000_000_000 + meta.mtime_nsec(),
            ctime_ns: meta.ctime() * 1_000_000_000 + meta.ctime_nsec(),
            ino: meta.ino(),
        }
    }
}

/// An entry's type, as its directory reports it: a symlink is not followed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    Dir,
    File,
    Symlink,
    Other,
}

impl Kind {
    fn of(kind: FileType) -> Self {
        if kind.is_dir() {
            Kind::Dir
        } else if kind.is_file() {
            Kind::File
        } else if kind.is_symlink() {
            Kind::Symlink
        } else {
            Kind::Other
        }
    }
}

/// One entry of a directory listing, with its kind and `lstat` when the
/// listing carried them; `None` is asked of the filesystem.
struct DirEntry {
    name: Vec<u8>,
    kind: Option<Kind>,
    stat: Option<Stat>,
}

/// `dir`'s entries: through getattrlistbulk on macOS where the filesystem
/// supports it, else readdir.
fn list_dir(dir: &Path) -> Result<Vec<DirEntry>, ScanError> {
    #[cfg(target_os = "macos")]
    if let Some(entries) = bulk::list(dir).map_err(|e| io_error(e, dir))? {
        return Ok(entries);
    }
    let mut out = Vec::new();
    for entry in fs::read_dir(dir).map_err(|e| io_error(e, dir))? {
        let entry = entry.map_err(|e| io_error(e, dir))?;
        let kind = entry.file_type().map_err(|e| io_error(e, &entry.path()))?;
        out.push(DirEntry {
            name: entry.file_name().as_bytes().to_vec(),
            kind: Some(Kind::of(kind)),
            stat: None,
        });
    }
    Ok(out)
}

struct WalkedFile {
    rel: String,
    stat: Stat,
}

struct Walked {
    files: Vec<WalkedFile>,
    /// Symlinks that are not files, with the bytes of their targets.
    links: Vec<(String, Vec<u8>)>,
}

/// Files under `root`, symlinked files stat'ed through, and every other
/// symlink -- to a directory, which is not followed, or dangling -- with the
/// bytes of its target. Other entries (FIFOs, sockets, devices) are not
/// content. `owner_marker` at the root is tether's, not content either.
fn walk(root: &Path, owner_marker: &str) -> Result<Walked, ScanError> {
    let mut files = Vec::new();
    let mut links = Vec::new();
    let mut stack = vec![(root.to_path_buf(), String::new())];
    while let Some((dir, dir_rel)) = stack.pop() {
        for entry in list_dir(&dir)? {
            let path = dir.join(OsStr::from_bytes(&entry.name));
            let Ok(name) = std::str::from_utf8(&entry.name) else {
                return Err(ScanError::NotUtf8 { path });
            };
            let rel = if dir_rel.is_empty() {
                name.to_owned()
            } else {
                format!("{dir_rel}/{name}")
            };
            let lstat = || fs::symlink_metadata(&path).map_err(|e| io_error(e, &path));
            let (kind, stat) = match entry.kind {
                Some(kind) => (kind, entry.stat),
                None => {
                    let meta = lstat()?;
                    (Kind::of(meta.file_type()), Some(Stat::of(&meta)))
                }
            };
            match kind {
                Kind::Dir => stack.push((path, rel)),
                Kind::File if rel == owner_marker => {}
                Kind::File => {
                    let stat = match stat {
                        Some(stat) => stat,
                        None => Stat::of(&lstat()?),
                    };
                    files.push(WalkedFile { rel, stat });
                }
                // `DirEntry.is_file()` follows a symlink, and says "not a
                // file" only when the target does not exist; any other error
                // is raised.
                Kind::Symlink => match fs::metadata(&path) {
                    Ok(meta) if meta.is_file() => {
                        if rel != owner_marker {
                            files.push(WalkedFile {
                                rel,
                                stat: Stat::of(&meta),
                            });
                        }
                    }
                    Err(e) if e.kind() != io::ErrorKind::NotFound => {
                        return Err(io_error(e, &path))
                    }
                    _ => {
                        let target = fs::read_link(&path).map_err(|e| io_error(e, &path))?;
                        links.push((rel, target.as_os_str().as_bytes().to_vec()));
                    }
                },
                Kind::Other => {}
            }
        }
    }
    Ok(Walked { files, links })
}

#[cfg(target_os = "macos")]
mod bulk {
    //! getattrlistbulk(2): a directory's entries with their attributes in a
    //! handful of calls, where readdir and an lstat per entry cost a call
    //! each -- and, past the kernel's vnode cache (`kern.maxvnodes`), a slow
    //! one: 1.2 s against 4.9 s for a million files.

    use std::ffi::CString;
    use std::io;
    use std::mem::size_of;
    use std::os::unix::ffi::OsStrExt;
    use std::path::Path;

    use super::{DirEntry, Kind, Stat};

    const ATTR_CMN_ERROR: u32 = 0x2000_0000; // <sys/attr.h>; not in libc
    const VREG: u32 = 1;
    const VDIR: u32 = 2;
    const VLNK: u32 = 5;
    const BUFFER: usize = 256 * 1024;

    struct Fd(libc::c_int);

    impl Drop for Fd {
        fn drop(&mut self) {
            // SAFETY: the descriptor is open and owned here alone.
            unsafe { libc::close(self.0) };
        }
    }

    /// `T` at `at`, which the bounds check keeps inside `buf`.
    fn read<T: Copy>(buf: &[u8], at: usize) -> Option<T> {
        let end = at.checked_add(size_of::<T>())?;
        let bytes = buf.get(at..end)?;
        // SAFETY: `bytes` holds `size_of::<T>()` bytes, read unaligned, and
        // `T` is plain data for which any bit pattern is valid.
        Some(unsafe { std::ptr::read_unaligned(bytes.as_ptr().cast::<T>()) })
    }

    /// `Ok(None)` where the filesystem cannot answer (or answers in a shape
    /// this does not know): readdir then lists the directory instead.
    pub(super) fn list(dir: &Path) -> io::Result<Option<Vec<DirEntry>>> {
        let Ok(c_dir) = CString::new(dir.as_os_str().as_bytes()) else {
            return Ok(None);
        };
        // SAFETY: a NUL-terminated path; the result is checked below.
        let fd = unsafe {
            libc::open(
                c_dir.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        let fd = Fd(fd);
        // SAFETY: `attrlist` is plain data; zero is its empty request.
        let mut request: libc::attrlist = unsafe { std::mem::zeroed() };
        request.bitmapcount = libc::ATTR_BIT_MAP_COUNT;
        request.commonattr = libc::ATTR_CMN_RETURNED_ATTRS
            | ATTR_CMN_ERROR
            | libc::ATTR_CMN_NAME
            | libc::ATTR_CMN_OBJTYPE
            | libc::ATTR_CMN_MODTIME
            | libc::ATTR_CMN_CHGTIME
            | libc::ATTR_CMN_FILEID;
        request.fileattr = libc::ATTR_FILE_DATALENGTH;
        let mut buf = vec![0u8; BUFFER];
        let mut out = Vec::new();
        loop {
            // SAFETY: `request` and `buf` outlive the call, and `buf.len()`
            // is the size of the buffer passed.
            let count = unsafe {
                libc::getattrlistbulk(
                    fd.0,
                    (&mut request as *mut libc::attrlist).cast(),
                    buf.as_mut_ptr().cast(),
                    buf.len(),
                    libc::FSOPT_PACK_INVAL_ATTRS as u64,
                )
            };
            if count < 0 {
                let error = io::Error::last_os_error();
                return match error.raw_os_error() {
                    Some(libc::ENOTSUP | libc::EINVAL) => Ok(None),
                    _ => Err(error),
                };
            }
            if count == 0 {
                return Ok(Some(out));
            }
            let mut at = 0;
            for _ in 0..count {
                let Some(length) = read::<u32>(&buf, at).map(|n| n as usize) else {
                    return Ok(None);
                };
                let Some(entry) = buf.get(at..at + length).and_then(entry) else {
                    return Ok(None);
                };
                out.push(entry);
                at += length;
            }
        }
    }

    /// One packed entry: its length; the attributes returned; then, every
    /// requested one packed (`FSOPT_PACK_INVAL_ATTRS`), the error -- which
    /// comes before the other common attributes -- name, type, mtime, ctime,
    /// file id and data length. `None` when it is not that shape.
    fn entry(e: &[u8]) -> Option<DirEntry> {
        let mut at = 4;
        let returned: libc::attribute_set_t = read(e, at)?;
        at += size_of::<libc::attribute_set_t>();
        let error: u32 = read(e, at)?;
        at += 4;
        let name_ref: libc::attrreference_t = read(e, at)?;
        let name_start = at.checked_add_signed(name_ref.attr_dataoffset as isize)?;
        let raw = e.get(name_start..name_start.checked_add(name_ref.attr_length as usize)?)?;
        at += size_of::<libc::attrreference_t>();
        let objtype: u32 = read(e, at)?;
        at += 4;
        let mtime: libc::timespec = read(e, at)?;
        at += size_of::<libc::timespec>();
        let ctime: libc::timespec = read(e, at)?;
        at += size_of::<libc::timespec>();
        let ino: u64 = read(e, at)?;
        at += 8;
        let size: i64 = read(e, at)?;
        if returned.commonattr & libc::ATTR_CMN_NAME == 0 {
            return None;
        }
        let name = raw[..raw.iter().position(|&b| b == 0).unwrap_or(raw.len())].to_vec();
        let common = libc::ATTR_CMN_OBJTYPE
            | libc::ATTR_CMN_MODTIME
            | libc::ATTR_CMN_CHGTIME
            | libc::ATTR_CMN_FILEID;
        let kind = match objtype {
            VDIR => Kind::Dir,
            VREG => Kind::File,
            VLNK => Kind::Symlink,
            _ => Kind::Other,
        };
        let known = error == 0 && returned.commonattr & common == common;
        let sized = kind != Kind::File || returned.fileattr & libc::ATTR_FILE_DATALENGTH != 0;
        Some(DirEntry {
            name,
            kind: known.then_some(kind),
            stat: (known && sized).then(|| Stat {
                size: size as u64,
                mtime_ns: mtime.tv_sec * 1_000_000_000 + mtime.tv_nsec,
                ctime_ns: ctime.tv_sec * 1_000_000_000 + ctime.tv_nsec,
                ino,
            }),
        })
    }
}

const READ_CHUNK: usize = 1 << 20;

thread_local! {
    static BUFFER: RefCell<Vec<u8>> = RefCell::new(vec![0; READ_CHUNK]);
}

fn hash_file(path: &Path) -> Result<[u8; 32], ScanError> {
    let mut file = File::open(path).map_err(|e| io_error(e, path))?;
    let mut hasher = Sha256::new();
    BUFFER.with(|buffer| -> Result<(), ScanError> {
        let mut buffer = buffer.borrow_mut();
        loop {
            match file.read(&mut buffer) {
                Ok(0) => return Ok(()),
                Ok(n) => hasher.update(&buffer[..n]),
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(io_error(e, path)),
            }
        }
    })?;
    Ok(hasher.finalize().into())
}

/// Hash `paths` on `workers` threads, results in the order of `paths`. The
/// threads live for this call only, like the Python scanner's pool: a process
/// that forks after a scan has no pool whose threads it lost.
fn hash_files(paths: &[PathBuf], workers: usize) -> Vec<Result<[u8; 32], ScanError>> {
    // APFS serializes concurrent opens: past four threads a scan of small
    // files slows down (100k files: 1.0 s on 4 threads, 2.6 s on 8, 5 s on 16).
    let workers = if cfg!(target_os = "macos") {
        workers.min(4)
    } else {
        workers
    };
    let workers = workers.clamp(1, paths.len().max(1));
    if workers == 1 {
        return paths.iter().map(|path| hash_file(path)).collect();
    }
    let next = AtomicUsize::new(0);
    let mut out: Vec<Option<Result<[u8; 32], ScanError>>> = Vec::new();
    out.resize_with(paths.len(), || None);
    thread::scope(|scope| {
        let threads: Vec<_> = (0..workers)
            .map(|_| {
                scope.spawn(|| {
                    let mut done = Vec::new();
                    loop {
                        let i = next.fetch_add(1, Ordering::Relaxed);
                        let Some(path) = paths.get(i) else {
                            return done;
                        };
                        done.push((i, hash_file(path)));
                    }
                })
            })
            .collect();
        for t in threads {
            for (i, result) in t.join().expect("a hashing thread panicked") {
                out[i] = Some(result);
            }
        }
    });
    out.into_iter()
        .map(|result| result.expect("every path was hashed"))
        .collect()
}

fn now_ns() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as i64)
        .unwrap_or(0)
}

/// One cache entry; on disk `[size, mtime_ns, ctime_ns, ino, sha256, hashed_at_ns]`.
#[derive(Clone, Copy, Debug)]
struct Entry {
    stat: Stat,
    digest: [u8; 32],
    hashed_at_ns: i64,
}

impl Entry {
    /// The Python cache's `lookup`: every stat field matches, and the entry
    /// was not taken so soon after the file's last change that a write in the
    /// same timestamp tick could hide behind it (`_racy`).
    fn vouches_for(&self, stat: &Stat, racy_window_ns: i64) -> bool {
        self.stat == *stat && self.hashed_at_ns - stat.mtime_ns.max(stat.ctime_ns) >= racy_window_ns
    }
}

impl Serialize for Entry {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut t = serializer.serialize_tuple(6)?;
        t.serialize_element(&self.stat.size)?;
        t.serialize_element(&self.stat.mtime_ns)?;
        t.serialize_element(&self.stat.ctime_ns)?;
        t.serialize_element(&self.stat.ino)?;
        t.serialize_element(&hex::encode(self.digest))?;
        t.serialize_element(&self.hashed_at_ns)?;
        t.end()
    }
}

impl<'de> Deserialize<'de> for Entry {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct EntryVisitor;

        impl<'de> Visitor<'de> for EntryVisitor {
            type Value = Entry;

            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("[size, mtime_ns, ctime_ns, ino, sha256, hashed_at_ns]")
            }

            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Entry, A::Error> {
                let missing = || de::Error::custom("a cache entry has six fields");
                let size = seq.next_element()?.ok_or_else(missing)?;
                let mtime_ns = seq.next_element()?.ok_or_else(missing)?;
                let ctime_ns = seq.next_element()?.ok_or_else(missing)?;
                let ino = seq.next_element()?.ok_or_else(missing)?;
                let text: &str = seq.next_element()?.ok_or_else(missing)?;
                let hashed_at_ns = seq.next_element()?.ok_or_else(missing)?;
                if seq.next_element::<de::IgnoredAny>()?.is_some() {
                    return Err(missing());
                }
                let mut digest = [0u8; 32];
                hex::decode_to_slice(text, &mut digest).map_err(de::Error::custom)?;
                Ok(Entry {
                    stat: Stat {
                        size,
                        mtime_ns,
                        ctime_ns,
                        ino,
                    },
                    digest,
                    hashed_at_ns,
                })
            }
        }

        deserializer.deserialize_seq(EntryVisitor)
    }
}

struct State {
    entries: Option<HashMap<String, Entry>>,
    dirty: bool,
}

/// A file's key, as the Python cache spells `str(root / rel)` (`root_key`
/// alone for a single file's empty `rel`). pathlib joins with `/` and drops a
/// root of `.` (`Path(".") / "a"` is `"a"`); that is the only root not
/// already in its own spelling, since `local_path` normalizes a relative root
/// lexically and resolves an absolute one.
fn key_into(key: &mut String, root_key: &str, rel: &str) {
    key.clear();
    if rel.is_empty() {
        key.push_str(root_key);
        return;
    }
    if root_key != "." {
        key.push_str(root_key);
        if !root_key.ends_with('/') {
            key.push('/');
        }
    }
    key.push_str(rel);
}

/// Path -> content hash, persisted as JSON at `path` (in memory only without
/// one), shared by every concurrent fingerprint of a backend. Lookups and
/// records hold the lock; hashing does not.
pub struct Cache {
    path: Option<PathBuf>,
    state: Mutex<State>,
    hashed: AtomicU64,
}

impl Cache {
    pub fn new(path: Option<PathBuf>) -> Self {
        Cache {
            path,
            state: Mutex::new(State {
                entries: None,
                dirty: false,
            }),
            hashed: AtomicU64::new(0),
        }
    }

    /// Files read since construction.
    pub fn hashed(&self) -> u64 {
        self.hashed.load(Ordering::Relaxed)
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// The entries, read from disk on first use. A file this cannot read whole
    /// -- missing, another format, corrupt -- counts as empty, as the Python
    /// cache treats one it cannot parse: every file is read once more.
    fn entries<'a>(&self, state: &'a mut State) -> &'a mut HashMap<String, Entry> {
        state.entries.get_or_insert_with(|| {
            self.path
                .as_ref()
                .and_then(|path| fs::read(path).ok())
                .and_then(|bytes| serde_json::from_slice(&bytes).ok())
                .unwrap_or_default()
        })
    }

    /// The content hash of each file `rel` under `root` (whose key is
    /// `root_key`), reading only the files the cache cannot vouch for,
    /// `workers` at a time; then the cache is saved if it changed.
    fn hashes(
        &self,
        root: &Path,
        root_key: &str,
        files: &[WalkedFile],
        racy_window_ns: i64,
        workers: usize,
    ) -> Result<Vec<[u8; 32]>, ScanError> {
        let mut out: Vec<Option<[u8; 32]>> = vec![None; files.len()];
        let mut misses = Vec::new();
        let mut key = String::with_capacity(root_key.len() + 64);
        {
            let mut state = self.lock();
            let entries = self.entries(&mut state);
            for (i, file) in files.iter().enumerate() {
                key_into(&mut key, root_key, &file.rel);
                match entries.get(key.as_str()) {
                    Some(entry) if entry.vouches_for(&file.stat, racy_window_ns) => {
                        out[i] = Some(entry.digest)
                    }
                    _ => misses.push(i),
                }
            }
        }
        if !misses.is_empty() {
            let paths: Vec<PathBuf> = misses
                .iter()
                .map(|&i| match files[i].rel.as_str() {
                    "" => root.to_path_buf(),
                    rel => root.join(rel),
                })
                .collect();
            // The first failure, in file order, is the one raised, and nothing
            // read alongside it is recorded.
            let read: Vec<[u8; 32]> = hash_files(&paths, workers)
                .into_iter()
                .collect::<Result<_, _>>()?;
            let hashed_at_ns = now_ns();
            let mut state = self.lock();
            let entries = self.entries(&mut state);
            for (&i, digest) in misses.iter().zip(read) {
                key_into(&mut key, root_key, &files[i].rel);
                entries.insert(
                    key.clone(),
                    Entry {
                        stat: files[i].stat,
                        digest,
                        hashed_at_ns,
                    },
                );
                out[i] = Some(digest);
            }
            state.dirty = true;
            self.hashed
                .fetch_add(misses.len() as u64, Ordering::Relaxed);
        }
        self.save()?;
        Ok(out
            .into_iter()
            .map(|d| d.expect("every file was found or read"))
            .collect())
    }

    /// Write the cache to a temporary file beside it, then move it into place.
    pub fn save(&self) -> Result<(), ScanError> {
        let mut state = self.lock();
        let (Some(path), true) = (self.path.as_ref(), state.dirty) else {
            return Ok(());
        };
        let Some(entries) = state.entries.as_ref() else {
            return Ok(());
        };
        let dir = path.parent().unwrap_or(Path::new("."));
        fs::create_dir_all(dir).map_err(|e| io_error(e, dir))?;
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        let tmp = dir.join(format!(".{name}.{}.tmp", std::process::id()));
        let write = || -> io::Result<()> {
            let mut out = BufWriter::with_capacity(1 << 20, File::create(&tmp)?);
            serde_json::to_writer(&mut out, entries)?;
            out.flush()
        };
        write().map_err(|e| io_error(e, &tmp))?;
        fs::rename(&tmp, path).map_err(|e| io_error(e, path))?;
        state.dirty = false;
        Ok(())
    }

    /// A directory: `(count, size, digest, listing)` as `_fingerprint_local`
    /// computes them.
    pub fn fingerprint_dir(
        &self,
        root: &Path,
        owner_marker: &str,
        racy_window_ns: i64,
        workers: usize,
    ) -> Result<(usize, u64, String, Listing), ScanError> {
        let root_key = utf8(root)?;
        // The cache file is read before the walk, so its bytes are freed
        // before the walk's entries pile up.
        self.entries(&mut self.lock());
        let Walked { files, links } = walk(root, owner_marker)?;
        let digests = self.hashes(root, root_key, &files, racy_window_ns, workers)?;
        let size = files.iter().map(|f| f.stat.size).sum();
        let mut rows: Vec<Row> = files
            .into_iter()
            .zip(digests)
            .map(|(f, digest)| Row {
                path: f.rel,
                token: Token::File(digest),
                size: f.stat.size,
            })
            .collect();
        rows.extend(links.into_iter().map(|(rel, target)| Row {
            path: rel,
            token: Token::Link(Sha256::digest(&target).into()),
            size: 0,
        }));
        let listing = Listing::new(rows);
        Ok((listing.len(), size, listing.digest(), listing))
    }

    /// A single file: `(size, sha256)`.
    pub fn fingerprint_file(
        &self,
        path: &Path,
        racy_window_ns: i64,
    ) -> Result<(u64, String), ScanError> {
        let key = utf8(path)?;
        let stat = Stat::of(&fs::metadata(path).map_err(|e| io_error(e, path))?);
        let file = [WalkedFile {
            rel: String::new(),
            stat,
        }];
        let digests = self.hashes(path, key, &file, racy_window_ns, 1)?;
        Ok((stat.size, hex::encode(digests[0])))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    #[test]
    fn walks_like_the_python_scanner() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().canonicalize().unwrap();
        fs::create_dir_all(root.join("sub/deep")).unwrap();
        fs::write(root.join("a.csv"), b"a,b\n1,2\n").unwrap();
        fs::write(root.join("sub/c.csv"), b"c\n").unwrap();
        fs::write(root.join("sub/deep/d.bin"), (0..=255u8).collect::<Vec<_>>()).unwrap();
        fs::write(root.join("é.txt"), b"accent\n").unwrap();
        fs::write(root.join("empty.txt"), b"").unwrap();
        symlink("a.csv", root.join("link-to-a")).unwrap();
        symlink("sub", root.join("link-to-sub")).unwrap();
        symlink("nowhere", root.join("dangling")).unwrap();
        fs::write(root.join(".tether-owner"), b"22620a34\n").unwrap();

        let cache = Cache::new(None);
        let (count, size, digest, listing) =
            cache.fingerprint_dir(&root, ".tether-owner", 0, 4).unwrap();
        // tests/test_golden.py::test_directory_fingerprint pins the same tree.
        assert_eq!((count, size), (8, 281));
        assert_eq!(digest, "5ab26439559322b53fc2d541e7850724");
        assert!(listing.text().ends_with(
            "{\"p\":\"\\u00e9.txt\",\"k\":\
             \"8f8df9963c9628741bfeeac7efb739164d0858fd03eb1950f385bb26512cef55\",\"s\":7}\n"
        ));
        assert_eq!(cache.hashed(), 6);
        cache.fingerprint_dir(&root, ".tether-owner", 0, 4).unwrap();
        assert_eq!(cache.hashed(), 6);
    }

    #[test]
    fn the_listing_carries_what_lstat_says() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().canonicalize().unwrap();
        fs::create_dir(root.join("sub")).unwrap();
        fs::write(root.join("f"), b"abc").unwrap();
        fs::write(root.join("empty"), b"").unwrap();
        fs::hard_link(root.join("f"), root.join("hard")).unwrap();
        symlink("f", root.join("link")).unwrap();
        symlink("nowhere", root.join("dangling")).unwrap();
        fs::write(root.join("é"), b"x").unwrap();
        let mut seen = 0;
        for entry in list_dir(&root).unwrap() {
            let path = root.join(OsStr::from_bytes(&entry.name));
            let meta = fs::symlink_metadata(&path).unwrap();
            assert_eq!(entry.kind.unwrap(), Kind::of(meta.file_type()), "{path:?}");
            // readdir leaves the stat to the walk. On macOS getattrlistbulk
            // must supply it: a layout its parser stopped recognizing falls
            // back to readdir, and every comparison with Python still passes.
            if meta.is_file() && (cfg!(target_os = "macos") || entry.stat.is_some()) {
                assert_eq!(entry.stat, Some(Stat::of(&meta)), "{path:?}");
            }
            seen += 1;
        }
        assert_eq!(seen, 7);
    }

    #[test]
    fn keys_are_spelled_as_pathlib_joins_them() {
        let mut key = String::new();
        for (root, rel, want) in [
            (".", "a/b", "a/b"),
            ("tree", "a", "tree/a"),
            ("../tree", "a", "../tree/a"),
            ("/data/tree", "a/b", "/data/tree/a/b"),
            ("/", "a", "/a"),
            ("/data/f.bin", "", "/data/f.bin"),
        ] {
            key_into(&mut key, root, rel);
            assert_eq!(key, want, "{root:?} / {rel:?}");
        }
    }

    #[test]
    fn a_racy_entry_is_read_again_and_the_cache_survives_a_new_process() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().canonicalize().unwrap();
        let file = root.join("f.bin");
        fs::write(&file, b"old!").unwrap();
        let cache_path = root.join("cache/file-hashes.json");
        let cache = Cache::new(Some(cache_path.clone()));
        cache.fingerprint_file(&file, 2_000_000_000).unwrap();
        cache.fingerprint_file(&file, 2_000_000_000).unwrap();
        assert_eq!(cache.hashed(), 2); // hashed right after the change: racy
        cache.fingerprint_file(&file, 0).unwrap();
        assert_eq!(cache.hashed(), 2);
        let again = Cache::new(Some(cache_path.clone()));
        again.fingerprint_file(&file, 0).unwrap();
        assert_eq!(again.hashed(), 0);
        let on_disk: serde_json::Value =
            serde_json::from_slice(&fs::read(&cache_path).unwrap()).unwrap();
        assert_eq!(on_disk[file.to_str().unwrap()].as_array().unwrap().len(), 6);
    }
}
