//! Windows-only personal builds from the owner's `origin/chey` forks.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::Arc;
use std::sync::mpsc::{Receiver, TryRecvError, channel};

use artcraft_launcher_core::catalog::AppKind;
use eframe::egui;
use serde::Deserialize;

const DEFAULT_ROOT: &str = r"E:\Users\cheyr\storytold forks";
const DEFAULT_PYTHON: &str = "python.exe";
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum View {
    Personal,
    Official,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Action {
    Status,
    Fork,
    Build,
    Open,
}

impl Action {
    fn argument(self) -> &'static str {
        match self {
            Self::Status => "status",
            Self::Fork => "fork",
            Self::Build => "build",
            Self::Open => "open",
        }
    }
    fn mutates(self) -> bool {
        matches!(self, Self::Fork | Self::Build)
    }
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
enum ForkState {
    New,
    NeedsClone,
    NeedsBranch,
    Ready,
    Error,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
struct StatusRecord {
    repo: String,
    state: ForkState,
    remote_sha: Option<String>,
    installed_sha: Option<String>,
    detail: String,
    #[serde(default)]
    build_supported: bool,
}

struct Running {
    action: Action,
    repo: Option<String>,
    repos: Vec<String>,
    receiver: Receiver<Result<Output, String>>,
}

pub struct PersonalForks {
    pub view: View,
    root: PathBuf,
    python: PathBuf,
    helper: PathBuf,
    rows: HashMap<String, StatusRecord>,
    running: Option<Running>,
    wake: Option<Arc<dyn Fn() + Send + Sync>>,
    error: Option<String>,
    notice: Option<String>,
    refresh_after_mutation: bool,
}

impl PersonalForks {
    pub fn new() -> Self {
        let explicit_root = std::env::var_os("STORYTOLD_FORKS_ROOT");
        let root = explicit_root.as_ref().map_or_else(|| PathBuf::from(DEFAULT_ROOT), PathBuf::from);
        let python = std::env::var_os("STORYTOLD_PYTHON").map_or_else(|| PathBuf::from(DEFAULT_PYTHON), PathBuf::from);
        let helper = root.join("storytold_install.py");
        let view = if explicit_root.is_some() || helper.is_file() { View::Personal } else { View::Official };
        Self { view, root, python, helper, rows: HashMap::new(), running: None, wake: None, error: None, notice: None, refresh_after_mutation: false }
    }

    pub fn start(&mut self, wake: Arc<dyn Fn() + Send + Sync>) {
        self.wake = Some(wake);
        if self.view == View::Personal {
            self.refresh();
        }
    }

    pub fn personal(&self) -> bool {
        self.view == View::Personal
    }

    pub fn mutation_active(&self) -> bool {
        self.running.as_ref().is_some_and(|work| work.action.mutates())
    }

    pub fn tick(&mut self) {
        let result = match self.running.as_ref().map(|work| work.receiver.try_recv()) {
            Some(Ok(result)) => Some(result),
            Some(Err(TryRecvError::Disconnected)) => Some(Err("helper worker disconnected before returning a result".to_string())),
            Some(Err(TryRecvError::Empty)) | None => None,
        };
        let Some(result) = result else { return };
        let Some(work) = self.running.take() else { return };
        match result {
            Ok(output) if work.action == Action::Status => self.finish_status(output, &work.repos),
            Ok(output) => self.finish_action(work.action, work.repo.as_deref(), output),
            Err(error) => {
                if work.action == Action::Status {
                    self.rows.clear();
                }
                self.error = Some(error);
            }
        }
        if self.refresh_after_mutation && self.running.is_none() {
            self.refresh_after_mutation = false;
            self.refresh();
        }
    }

    pub fn switch(&mut self, view: View) {
        if view == View::Official && self.mutation_active() {
            return;
        }
        self.view = view;
        if view == View::Personal && self.rows.is_empty() && self.running.is_none() {
            self.refresh();
        }
    }

    pub fn draw_switch(&mut self, ctx: &egui::Context) {
        egui::Area::new(egui::Id::unique("storytold-view-switch")).order(egui::Order::Foreground).anchor(egui::Align2::CENTER_TOP, [0.0, 8.0]).show(
            ctx,
            |ui| {
                egui::Frame::new().corner_radius(5).inner_margin(egui::Margin::symmetric(5, 3)).show(ui, |ui| {
                    ui.horizontal(|ui| {
                        let personal = ui.selectable_label(self.view == View::Personal, "Personal forks");
                        if personal.clicked() {
                            self.switch(View::Personal);
                        }
                        let official =
                            ui.add_enabled(!self.mutation_active(), egui::Button::selectable(self.view == View::Official, "Official releases"));
                        if official.clicked() {
                            self.switch(View::Official);
                        }
                        if self.mutation_active() {
                            official.on_disabled_hover_text("Wait for the personal build to finish");
                        }
                    });
                });
            },
        );
    }

    pub fn draw_header(&self, ctx: &egui::Context) {
        let screen = ctx.content_rect();
        let size = egui::vec2(screen.width(), 48.0);
        egui::Area::new(egui::Id::unique("storytold-personal-header")).order(egui::Order::Foreground).fixed_pos(screen.left_top()).show(ctx, |ui| {
            ui.set_min_size(size);
            ui.set_max_size(size);
            let rect = ui.max_rect();
            ui.painter().rect_filled(rect, 0.0, ui.visuals().panel_fill);
            let drag = ui.interact(rect, egui::Id::unique("storytold-personal-title-drag"), egui::Sense::click_and_drag());
            if drag.drag_started_by(egui::PointerButton::Primary) {
                ctx.send_viewport_cmd(egui::ViewportCommand::StartDrag);
            }
            if drag.double_clicked() {
                let maximized = ctx.input(|input| input.viewport().maximized.unwrap_or(false));
                ctx.send_viewport_cmd(egui::ViewportCommand::Maximized(!maximized));
            }
            ui.horizontal(|ui| {
                ui.add_space(14.0);
                ui.strong("ArtCraft Launcher");
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    let close = ui.add_enabled(!self.mutation_active(), egui::Button::new("Close"));
                    if close.clicked() {
                        ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                    }
                    if self.mutation_active() {
                        close.on_disabled_hover_text("Wait for the personal build to finish");
                    }
                    let maximized = ctx.input(|input| input.viewport().maximized.unwrap_or(false));
                    let label = if maximized { "Restore" } else { "Maximize" };
                    if ui.button(label).clicked() {
                        ctx.send_viewport_cmd(egui::ViewportCommand::Maximized(!maximized));
                    }
                    if ui.button("Minimize").clicked() {
                        ctx.send_viewport_cmd(egui::ViewportCommand::Minimized(true));
                    }
                });
            });
        });
    }

    pub fn draw_personal(&mut self, ctx: &egui::Context, release_busy: bool) {
        if !self.personal() {
            return;
        }
        let screen = ctx.content_rect();
        let top = screen.top() + 48.0;
        let size = egui::vec2(screen.width(), (screen.bottom() - top).max(0.0));
        egui::Area::new(egui::Id::unique("storytold-personal-forks")).order(egui::Order::Foreground).fixed_pos(egui::pos2(screen.left(), top)).show(
            ctx,
            |ui| {
                ui.set_min_size(size);
                ui.set_max_size(size);
                let fill = ui.visuals().panel_fill;
                egui::Frame::new().fill(fill).inner_margin(egui::Margin::symmetric(20, 18)).show(ui, |ui| {
                    ui.set_min_size(egui::vec2((size.x - 40.0).max(0.0), (size.y - 36.0).max(0.0)));
                    self.personal_contents(ui, release_busy);
                });
            },
        );
    }

    fn personal_contents(&mut self, ui: &mut egui::Ui, release_busy: bool) {
        ui.horizontal(|ui| {
            if ui.add_enabled(self.running.is_none(), egui::Button::new("Refresh")).clicked() {
                self.refresh();
            }
            if let Some(work) = &self.running {
                ui.spinner();
                let text = match (&work.action, &work.repo) {
                    (Action::Status, _) => "Checking origin/chey".to_string(),
                    (action, Some(repo)) => format!("{} {repo}", action.argument()),
                    _ => "Working".to_string(),
                };
                ui.label(text);
            } else if let Some(notice) = &self.notice {
                ui.label(notice);
            }
        });
        if let Some(error) = &self.error {
            ui.colored_label(ui.visuals().error_fg_color, error);
        }
        if release_busy {
            ui.label("Finish the official release operation before starting a personal fork action.");
        }
        ui.add_space(12.0);
        egui::ScrollArea::vertical().auto_shrink(false).show(ui, |ui| {
            for row in catalog_rows() {
                self.draw_row(ui, &row, release_busy);
                ui.separator();
            }
        });
    }

    fn draw_row(&mut self, ui: &mut egui::Ui, row: &CatalogRow, release_busy: bool) {
        let status = self.rows.get(&row.repo).cloned();
        ui.horizontal(|ui| {
            ui.vertical(|ui| {
                ui.strong(&row.name);
                match status.as_ref() {
                    None => {
                        ui.label("Status unavailable");
                    }
                    Some(record) => self.status_text(ui, record),
                }
            });
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let enabled = self.running.is_none() && !release_busy;
                if !row.launcher
                    && status.as_ref().is_some_and(|record| record.installed_sha.is_some())
                    && status.as_ref().is_some_and(|record| matches!(record.state, ForkState::Ready | ForkState::Error))
                {
                    self.action_button(ui, enabled, "Open", Action::Open, &row.repo);
                }
                match status.as_ref().map(|record| &record.state) {
                    Some(ForkState::New) => self.action_button(ui, enabled, "Fork", Action::Fork, &row.repo),
                    Some(ForkState::NeedsClone) => self.action_button(ui, enabled, "Clone", Action::Fork, &row.repo),
                    Some(ForkState::NeedsBranch) => self.action_button(ui, enabled, "Create chey branch", Action::Fork, &row.repo),
                    Some(ForkState::Ready) => {
                        if let Some(record) = status.as_ref() {
                            let same = record.remote_sha.is_some() && record.remote_sha == record.installed_sha;
                            if !record.build_supported {
                                ui.label("Windows build recipe unavailable");
                            } else if same {
                                ui.label("Up to date");
                            } else {
                                let label = if record.installed_sha.is_some() { "Update from chey" } else { "Install from chey" };
                                self.action_button(ui, enabled, label, Action::Build, &row.repo);
                            }
                        }
                    }
                    Some(ForkState::Error) | None => {}
                }
            });
        });
    }

    fn status_text(&self, ui: &mut egui::Ui, record: &StatusRecord) {
        match record.state {
            ForkState::New => {
                ui.label("New");
            }
            ForkState::NeedsClone => {
                ui.label("Fork exists; clone is missing");
            }
            ForkState::NeedsBranch => {
                ui.label("Clone exists; chey branch is missing");
            }
            ForkState::Ready => {
                let remote = record.remote_sha.as_deref().map(short_sha).unwrap_or("unknown");
                let installed = record.installed_sha.as_deref().map(short_sha).unwrap_or("Not installed");
                ui.label(format!("chey {remote} · installed {installed}"));
            }
            ForkState::Error => {
                ui.colored_label(ui.visuals().error_fg_color, if record.detail.is_empty() { "Helper reported an error" } else { &record.detail });
            }
        }
    }

    fn action_button(&mut self, ui: &mut egui::Ui, enabled: bool, label: &str, action: Action, repo: &str) {
        if ui.add_enabled(enabled, egui::Button::new(label)).clicked() {
            self.run(action, vec![repo.to_string()], Some(repo.to_string()));
        }
    }

    fn refresh(&mut self) {
        if self.running.is_some() {
            return;
        }
        let repos = catalog_rows().into_iter().map(|row| row.repo).collect();
        self.run(Action::Status, repos, None);
    }

    fn run(&mut self, action: Action, repos: Vec<String>, repo: Option<String>) {
        if self.running.is_some() {
            return;
        }
        let known: HashSet<String> = catalog_rows().into_iter().map(|row| row.repo).collect();
        if repos.is_empty() || repos.iter().any(|candidate| !known.contains(candidate)) {
            self.error = Some("Refusing an unknown or empty repository request".to_string());
            return;
        }
        self.error = None;
        if action != Action::Status {
            self.notice = None;
        }
        let (sender, receiver) = channel();
        let python = self.python.clone();
        let helper = self.helper.clone();
        let root = self.root.clone();
        let wake = self.wake.clone();
        let worker_repos = repos.clone();
        std::thread::spawn(move || {
            let result = invoke(&python, &helper, &root, action, &worker_repos);
            let _ = sender.send(result);
            if let Some(wake) = wake {
                wake();
            }
        });
        self.running = Some(Running { action, repo, repos, receiver });
    }

    fn finish_status(&mut self, output: Output, expected: &[String]) {
        match status_from_output(output, expected) {
            Ok(rows) => self.rows = rows.into_iter().map(|row| (row.repo.clone(), row)).collect(),
            Err(error) => {
                self.rows.clear();
                self.error = Some(error);
            }
        }
    }

    fn finish_action(&mut self, action: Action, repo: Option<&str>, output: Output) {
        if !output.status.success() {
            self.error = Some(output_error(&output));
            return;
        }
        let message = String::from_utf8_lossy(&output.stdout).trim().to_string();
        self.notice = Some(if action == Action::Build && repo == Some("craft-launcher") {
            "Launcher build complete. Fully exit, then reopen using the Storytold Launcher shortcut.".to_string()
        } else if message.is_empty() {
            format!("{} completed", action.argument())
        } else {
            message
        });
        if action.mutates() {
            self.refresh_after_mutation = true;
        }
    }
}

#[derive(Clone)]
struct CatalogRow {
    repo: String,
    name: String,
    launcher: bool,
}

fn catalog_rows() -> Vec<CatalogRow> {
    let mut rows: Vec<CatalogRow> = artcraft_launcher_core::hosted_catalog::apps()
        .iter()
        .filter(|app| app.kind == AppKind::Official)
        .filter_map(|app| {
            app.repo.strip_prefix("storytold/").map(|repo| CatalogRow { repo: repo.to_string(), name: app.name.to_string(), launcher: false })
        })
        .collect();
    rows.push(CatalogRow { repo: "craft-launcher".to_string(), name: "Storytold Launcher".to_string(), launcher: true });
    rows
}

fn invoke(python: &Path, helper: &Path, root: &Path, action: Action, repos: &[String]) -> Result<Output, String> {
    use std::os::windows::process::CommandExt;

    let mut command = Command::new(python);
    command.env("PYTHONUTF8", "1").arg(helper).arg("--root").arg(root).arg(action.argument()).args(repos).creation_flags(CREATE_NO_WINDOW);
    command.output().map_err(|error| format!("Could not run {}: {error}", helper.display()))
}

fn status_from_output(output: Output, expected: &[String]) -> Result<Vec<StatusRecord>, String> {
    if !output.status.success() {
        return Err(output_error(&output));
    }
    let rows: Vec<StatusRecord> = serde_json::from_slice(&output.stdout).map_err(|error| format!("Malformed helper status JSON: {error}"))?;
    let expected: HashSet<&str> = expected.iter().map(String::as_str).collect();
    let mut seen = HashSet::new();
    for row in &rows {
        if !expected.contains(row.repo.as_str()) || !seen.insert(row.repo.as_str()) {
            return Err(format!("Helper returned an unknown or duplicate repository: {}", row.repo));
        }
        if (row.state == ForkState::Ready && row.remote_sha.as_deref().is_none_or(str::is_empty))
            || row.remote_sha.as_deref().is_some_and(str::is_empty)
            || row.installed_sha.as_deref().is_some_and(str::is_empty)
        {
            return Err(format!("Helper returned an invalid commit status for {}", row.repo));
        }
    }
    if seen.len() != expected.len() {
        return Err("Helper status omitted one or more requested repositories".to_string());
    }
    Ok(rows)
}

fn output_error(output: &Output) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    if stderr.is_empty() { format!("Helper exited with {}", output.status) } else { stderr }
}

fn short_sha(sha: &str) -> &str {
    sha.get(..12).unwrap_or(sha)
}

#[cfg(test)]
mod tests {
    use std::os::windows::process::ExitStatusExt;
    use std::process::ExitStatus;

    use super::*;

    fn output(code: u32, stdout: &str, stderr: &str) -> Output {
        Output { status: ExitStatus::from_raw(code), stdout: stdout.as_bytes().to_vec(), stderr: stderr.as_bytes().to_vec() }
    }

    #[test]
    fn ready_and_new_remain_distinct_and_keep_full_shas() -> Result<(), String> {
        let expected = vec!["photocraft".to_string(), "soundcraft".to_string()];
        let json = r#"[{"repo":"photocraft","state":"ready","remote_sha":"1234567890abcdef","installed_sha":"1234567890abcdef","detail":""},{"repo":"soundcraft","state":"new","remote_sha":null,"installed_sha":null,"detail":"missing fork"}]"#;
        let rows = status_from_output(output(0, json, ""), &expected)?;
        assert_eq!(rows[0].state, ForkState::Ready);
        assert_eq!(rows[0].remote_sha, rows[0].installed_sha);
        assert_eq!(rows[1].state, ForkState::New);
        Ok(())
    }

    #[test]
    fn failing_helper_uses_stderr_diagnostic() {
        let result = status_from_output(output(1, "", "network denied"), &["photocraft".to_string()]);
        assert_eq!(result, Err("network denied".to_string()));
    }
}
