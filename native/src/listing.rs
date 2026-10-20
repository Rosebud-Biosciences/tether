//! Directory listings as the file backend stores them, their digest, and the
//! diff of two stored listings -- byte for byte what `tether.backends.file`
//! writes (`_dump_listing`, `_digest_pairs`) and reads (`_load_listing`).

use std::collections::HashMap;
use std::fmt::Write as _;

use blake2::{Blake2b128, Digest};
use serde::Deserialize;

/// What "same content" means for one entry: a file's sha256, or the sha256 of
/// a symlink's target, which is what such an entry holds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Token {
    File([u8; 32]),
    Link([u8; 32]),
}

impl Token {
    pub fn text(&self) -> String {
        match self {
            Token::File(hash) => hex::encode(hash),
            Token::Link(hash) => format!("symlink:{}", hex::encode(hash)),
        }
    }
}

/// One entry: its path relative to the directory, `/`-separated; its token;
/// its size (a symlink's is 0).
#[derive(Clone, Debug)]
pub struct Row {
    pub path: String,
    pub token: Token,
    pub size: u64,
}

/// Rows sorted by path, the order both the listing and the digest use.
pub struct Listing {
    rows: Vec<Row>,
}

impl Listing {
    pub fn new(mut rows: Vec<Row>) -> Self {
        rows.sort_unstable_by(|a, b| a.path.cmp(&b.path));
        Self { rows }
    }

    pub fn len(&self) -> usize {
        self.rows.len()
    }

    pub fn is_empty(&self) -> bool {
        self.rows.is_empty()
    }

    /// BLAKE2b-128 over each `(path, token)`, each NUL-terminated.
    pub fn digest(&self) -> String {
        let mut hasher = Blake2b128::new();
        for row in &self.rows {
            hasher.update(row.path.as_bytes());
            hasher.update(b"\0");
            hasher.update(row.token.text().as_bytes());
            hasher.update(b"\0");
        }
        hex::encode(hasher.finalize())
    }

    /// The JSON lines a commit stores.
    pub fn text(&self) -> String {
        let mut out = String::with_capacity(self.rows.len() * 120);
        for row in &self.rows {
            push_row(&mut out, &row.path, &row.token.text(), row.size);
        }
        out
    }
}

/// `json.dumps({"p": path, "k": token, "s": size}, separators=(",", ":"))`
/// and a newline.
pub fn push_row(out: &mut String, path: &str, token: &str, size: u64) {
    out.push_str("{\"p\":");
    push_json_str(out, path);
    out.push_str(",\"k\":");
    push_json_str(out, token);
    out.push_str(",\"s\":");
    write!(out, "{size}").expect("writing to a String cannot fail");
    out.push_str("}\n");
}

/// A JSON string as Python's `json.dumps` writes it by default
/// (`ensure_ascii`): `"` and `\` escaped, `\b \f \n \r \t` short, anything
/// else outside printable ASCII as `\uXXXX` (lowercase, UTF-16 surrogate
/// pairs past the BMP), DEL included.
fn push_json_str(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            _ => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    write!(out, "\\u{unit:04x}").expect("writing to a String cannot fail");
                }
            }
        }
    }
    out.push('"');
}

#[derive(Deserialize)]
struct StoredRow {
    p: String,
    #[serde(default)]
    k: String,
    #[serde(default)]
    s: i64,
}

/// `_load_listing`: path -> (token, size), a later line winning. Anything it
/// cannot read the way Python would is an error, and the caller falls back to
/// the Python reader.
fn parse(text: &str) -> Result<HashMap<String, (String, i64)>, String> {
    let mut rows = HashMap::new();
    for line in text.lines() {
        if line.trim().is_empty() {
            continue;
        }
        let row: StoredRow = serde_json::from_str(line).map_err(|e| e.to_string())?;
        rows.insert(row.p, (row.k, row.s));
    }
    Ok(rows)
}

/// One changed path: `"added"`, `"removed"` or `"modified"`, and the byte
/// count `_human` shows for it (the size added, removed, or the change).
#[derive(Debug, PartialEq, Eq)]
pub struct Change {
    pub path: String,
    pub change: &'static str,
    pub bytes: i64,
}

/// Exact counts, the first `cap` changes in path order, and whether any
/// change did not fit.
#[derive(Debug, PartialEq, Eq)]
pub struct Diff {
    pub added: u64,
    pub removed: u64,
    pub modified: u64,
    pub entries: Vec<Change>,
    pub truncated: bool,
}

pub fn diff(a: &str, b: &str, cap: usize) -> Result<Diff, String> {
    let (rows_a, rows_b) = (parse(a)?, parse(b)?);
    let mut paths: Vec<&String> = rows_a
        .keys()
        .chain(rows_b.keys().filter(|p| !rows_a.contains_key(*p)))
        .collect();
    paths.sort_unstable();
    let mut out = Diff {
        added: 0,
        removed: 0,
        modified: 0,
        entries: Vec::new(),
        truncated: false,
    };
    for path in paths {
        let (change, bytes) = match (rows_a.get(path), rows_b.get(path)) {
            (None, Some(rb)) => ("added", rb.1),
            (Some(ra), None) => ("removed", ra.1.checked_neg().ok_or("size overflow")?),
            (Some(ra), Some(rb)) if ra != rb => {
                ("modified", rb.1.checked_sub(ra.1).ok_or("size overflow")?)
            }
            _ => continue,
        };
        match change {
            "added" => out.added += 1,
            "removed" => out.removed += 1,
            _ => out.modified += 1,
        }
        if out.entries.len() < cap {
            out.entries.push(Change {
                path: path.clone(),
                change,
                bytes,
            });
        } else {
            out.truncated = true;
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(path: &str, byte: u8, size: u64) -> Row {
        Row {
            path: path.to_owned(),
            token: Token::File([byte; 32]),
            size,
        }
    }

    #[test]
    fn escapes_like_python_json_dumps() {
        let mut out = String::new();
        push_json_str(&mut out, "a\"b\\c\n\t\u{1}\u{7f}é\u{2028}😀~ ");
        assert_eq!(
            out,
            r#""a\"b\\c\n\t\u0001\u007f\u00e9\u2028\ud83d\ude00~ ""#
        );
    }

    #[test]
    fn sorts_rows_and_writes_json_lines() {
        let listing = Listing::new(vec![row("sub/c.csv", 1, 12), row("a.csv", 2, 3)]);
        let a = hex::encode([2u8; 32]);
        let c = hex::encode([1u8; 32]);
        assert_eq!(
            listing.text(),
            format!(
                "{{\"p\":\"a.csv\",\"k\":\"{a}\",\"s\":3}}\n\
                 {{\"p\":\"sub/c.csv\",\"k\":\"{c}\",\"s\":12}}\n"
            )
        );
    }

    #[test]
    fn diff_counts_everything_and_caps_entries() {
        let a = "{\"p\":\"same\",\"k\":\"x\",\"s\":1}\n{\"p\":\"gone\",\"k\":\"y\",\"s\":5}\n\
                 {\"p\":\"edit\",\"k\":\"z\",\"s\":2}\n";
        let b = "{\"p\":\"same\",\"k\":\"x\",\"s\":1}\n{\"p\":\"edit\",\"k\":\"w\",\"s\":7}\n\
                 \n{\"p\":\"new\",\"s\":4}\n";
        let d = diff(a, b, 2).unwrap();
        assert_eq!(
            (d.added, d.removed, d.modified, d.truncated),
            (1, 1, 1, true)
        );
        assert_eq!(
            d.entries,
            vec![
                Change {
                    path: "edit".into(),
                    change: "modified",
                    bytes: 5
                },
                Change {
                    path: "gone".into(),
                    change: "removed",
                    bytes: -5
                },
            ]
        );
        assert!(diff("{\"k\":\"x\"}\n", "", 10).is_err());
    }
}
