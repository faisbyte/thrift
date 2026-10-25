mod util;
use crate::util::helper;

/// Parse the input.
pub fn parse(input: &str) -> Vec<u8> {
    input.bytes().collect()
}

pub struct Parser {
    pos: usize,
}

impl Parser {
    pub fn new() -> Self {
        Parser { pos: 0 }
    }

    fn advance(&mut self) {
        self.pos += 1;
    }
}

pub trait Visitor {
    fn visit(&self);
    fn done(&self) -> bool { true }
}
