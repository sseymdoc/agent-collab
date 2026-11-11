//! SSE 帧的增量解析（`docs/protocol.md` §8）。
//!
//! 帧格式：`id: <seq>\nevent: <type>\ndata: <json>\n\n`；以 `:` 开头的行是注释（心跳 `: ping`）。
//! 输入可以在任意字节处被切开，包括多字节字符和 CRLF 的中间。

/// 一条完整的 SSE 帧。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Frame {
    pub id: Option<String>,
    pub event: Option<String>,
    pub data: String,
}

impl Frame {
    /// 帧内三个字段的总字节数，调用方用它限制单帧大小。
    pub fn size(&self) -> usize {
        self.id.as_ref().map_or(0, String::len) + self.event.as_ref().map_or(0, String::len) + self.data.len()
    }
}

#[derive(Default)]
pub struct Parser {
    buf: Vec<u8>,
    frame: Frame,
    has_data: bool,
}

impl Parser {
    /// 喂入一块字节，返回其中已经完整的帧。
    pub fn push(&mut self, chunk: &[u8]) -> Vec<Frame> {
        self.buf.extend_from_slice(chunk);
        let mut frames = Vec::new();
        let mut start = 0;
        // ponytail: 行结束符只认 LF 与 CRLF（controller 只发 LF）；要支持裸 CR 时在这里加。
        while let Some(pos) = self.buf[start..].iter().position(|&b| b == b'\n') {
            let end = start + pos;
            let line = String::from_utf8_lossy(&self.buf[start..end]).into_owned();
            start = end + 1;
            if let Some(frame) = self.line(line.strip_suffix('\r').unwrap_or(&line)) {
                frames.push(frame);
            }
        }
        self.buf.drain(..start);
        frames
    }

    /// 尚未构成完整帧的缓冲字节数（未成行的字节加上已累积的 id、event、data），供调用方限制内存。
    pub fn pending(&self) -> usize {
        self.buf.len() + self.frame.size()
    }

    fn line(&mut self, line: &str) -> Option<Frame> {
        if line.is_empty() {
            // 空行结束一帧。与浏览器 EventSource 不同：只有 event 没有 data 的帧也交付，
            // 这样不带 data 的 `event: unauthorized` 不会被丢掉。id 不跨帧保留。
            let frame = std::mem::take(&mut self.frame);
            let had_data = std::mem::take(&mut self.has_data);
            return (had_data || frame.event.is_some()).then_some(frame);
        }
        if line.starts_with(':') {
            return None;
        }
        let (field, value) = line.split_once(':').unwrap_or((line, ""));
        let value = value.strip_prefix(' ').unwrap_or(value);
        match field {
            "id" if !value.contains('\0') => self.frame.id = Some(value.to_string()),
            "event" => self.frame.event = Some(value.to_string()),
            "data" => {
                if self.has_data {
                    self.frame.data.push('\n');
                }
                self.frame.data.push_str(value);
                self.has_data = true;
            }
            _ => {}
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(id: Option<&str>, event: Option<&str>, data: &str) -> Frame {
        Frame { id: id.map(Into::into), event: event.map(Into::into), data: data.into() }
    }

    const STREAM: &str = concat!(
        ": ping\n",
        "id: 41\nevent: job.created\ndata: {\"seq\":41,\"note\":\"任务 α\"}\n\n",
        ": ping\n",
        "id: 42\nevent: attempt.started\ndata: {\"seq\":42}\n\n",
        "event: snapshot_required\ndata: {\"min_seq\": 900}\n\n",
        "event: unauthorized\ndata: {}\n\n",
    );

    fn expected() -> Vec<Frame> {
        vec![
            frame(Some("41"), Some("job.created"), "{\"seq\":41,\"note\":\"任务 α\"}"),
            frame(Some("42"), Some("attempt.started"), "{\"seq\":42}"),
            frame(None, Some("snapshot_required"), "{\"min_seq\": 900}"),
            frame(None, Some("unauthorized"), "{}"),
        ]
    }

    #[test]
    fn multiple_frames_in_one_chunk() {
        assert_eq!(Parser::default().push(STREAM.as_bytes()), expected());
    }

    #[test]
    fn frames_split_at_every_byte_boundary() {
        let bytes = STREAM.as_bytes();
        for cut in 0..=bytes.len() {
            let mut p = Parser::default();
            let mut got = p.push(&bytes[..cut]);
            got.extend(p.push(&bytes[cut..]));
            assert_eq!(got, expected(), "cut at {cut}");
            assert_eq!(p.pending(), 0);
        }
    }

    #[test]
    fn one_byte_at_a_time() {
        let mut p = Parser::default();
        let got: Vec<Frame> = STREAM.as_bytes().iter().flat_map(|b| p.push(&[*b])).collect();
        assert_eq!(got, expected());
    }

    #[test]
    fn heartbeat_comments_produce_no_frames() {
        let mut p = Parser::default();
        assert!(p.push(b": ping\n").is_empty());
        assert!(p.push(b": ping\n\n: ping\n\n").is_empty());
        assert_eq!(p.pending(), 0);
    }

    #[test]
    fn unauthorized_without_data_is_delivered() {
        assert_eq!(
            Parser::default().push(b"event: unauthorized\n\n"),
            vec![frame(None, Some("unauthorized"), "")]
        );
    }

    #[test]
    fn crlf_multiline_data_and_bare_fields() {
        let mut p = Parser::default();
        assert_eq!(
            p.push(b"id:7\r\nevent:x\r\ndata:a\r\ndata: b\r\nretry: 5\r\nunknown\r\n\r\n"),
            vec![frame(Some("7"), Some("x"), "a\nb")]
        );
        // 只有 data 的帧没有 event；只有 id 的帧不交付，id 也不带到下一帧。
        assert_eq!(p.push(b"data: only\n\nid: 9\n\ndata: next\n\n"), vec![
            frame(None, None, "only"),
            frame(None, None, "next"),
        ]);
    }

    #[test]
    fn incomplete_tail_stays_pending() {
        let mut p = Parser::default();
        assert!(p.push(b"id: 1\nevent: x\ndata: {\"a\"").is_empty());
        assert!(p.pending() > 0);
        assert_eq!(p.push(b":1}\n\n"), vec![frame(Some("1"), Some("x"), "{\"a\":1}")]);
    }

    #[test]
    fn pending_counts_buffered_id_and_event() {
        let mut p = Parser::default();
        let long = "e".repeat(1000);
        assert!(p.push(format!("id: 12345\nevent: {long}\n").as_bytes()).is_empty());
        assert_eq!(p.pending(), 5 + 1000);
        assert_eq!(frame(Some("12"), Some("abc"), "data").size(), 9);
    }
}
