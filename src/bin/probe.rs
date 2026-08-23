use std::process::Command;
#[path = "../input.rs"]
mod input;
#[path = "../shell.rs"]
mod shell;

fn read_pointer() -> String {
    let out = Command::new("kdotool")
        .args(["getmouselocation", "--shell"])
        .envs(shell::session_env())
        .output()
        .expect("kdotool");
    let text = String::from_utf8_lossy(&out.stdout);
    let get = |k: &str| -> String {
        text.lines()
            .find(|l| l.starts_with(k))
            .and_then(|l| l.split('=').nth(1))
            .unwrap_or("?")
            .to_string()
    };
    format!("({},{})", get("X="), get("Y="))
}

fn main() -> anyhow::Result<()> {
    let scale = (2.0, 2.0);
    for (x, y) in [(200, 200), (1500, 700), (2600, 1100)] {
        input::move_to(x, y, scale)?;
        std::thread::sleep(std::time::Duration::from_millis(300));
        println!("asked ({x},{y}) -> landed {}", read_pointer());
    }
    Ok(())
}
