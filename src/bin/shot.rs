#[path = "../capture.rs"]
mod capture;
fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    capture::capture(&args[1], &args[2], None)
}
