//! Windows-only bridge from the verified Storytold helper to the shared launcher UI.

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::Arc;
use std::sync::mpsc::{Receiver, TryRecvError, channel};

use artcraft_launcher_core::catalog::AppKind;
use artcraft_launcher_ui_egui::{PersonalApp, PersonalRequest, PersonalState};
use serde::Deserialize;

const DEFAULT_ROOT: &str = r"E:\Users\cheyr\storytold forks";
const DEFAULT_PYTHON: &str = "python.exe";
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Action {
    Status,
    Install,
    Open,
}
impl Action {
    fn argument(self) -> &'static str {
        match self {
            Self::Status => "status",
            Self::Install => "install",
            Self::Open => "open",
        }
    }
    fn mutates(self) -> bool {
        self == Self::Install
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

#[derive(Clone, Debug, Deserialize)]
struct StatusRecord {
    repo: String,
    state: ForkState,
    remote_sha: Option<String>,
    installed_sha: Option<String>,
    #[serde(default)]
    installed_msi_version: Option<String>,
    detail: String,
    #[serde(default)]
    build_supported: bool,
}

impl StatusRecord {
    fn is_installed(&self) -> bool {
        self.installed_sha.is_some() || self.installed_msi_version.is_some()
    }
}

struct Work {
    action: Action,
    expected: Vec<String>,
    receiver: Receiver<Result<Output, String>>,
}

pub struct PersonalForks {
    enabled: bool,
    root: PathBuf,
    python: PathBuf,
    helper: PathBuf,
    rows: HashMap<String, StatusRecord>,
    known: bool,
    running: Option<Work>,
    queue: VecDeque<(Action, String, Vec<String>)>,
    wake: Option<Arc<dyn Fn() + Send + Sync>>,
    message: Option<(String, bool)>,
    refresh_when_idle: bool,
}

impl PersonalForks {
    pub fn new() -> Self {
        let explicit = std::env::var_os("STORYTOLD_FORKS_ROOT");
        let root = explicit.as_ref().map_or_else(|| PathBuf::from(DEFAULT_ROOT), PathBuf::from);
        let python = std::env::var_os("STORYTOLD_PYTHON").map_or_else(|| PathBuf::from(DEFAULT_PYTHON), PathBuf::from);
        let helper = root.join("storytold_install.py");
        let enabled = explicit.is_some() || helper.is_file();
        Self {
            enabled,
            root,
            python,
            helper,
            rows: HashMap::new(),
            known: false,
            running: None,
            queue: VecDeque::new(),
            wake: None,
            message: None,
            refresh_when_idle: false,
        }
    }

    pub fn enabled(&self) -> bool {
        self.enabled
    }
    pub fn mutation_active(&self) -> bool {
        self.running.as_ref().is_some_and(|w| w.action.mutates()) || self.queue.iter().any(|q| q.0.mutates())
    }
    pub fn working(&self) -> bool {
        self.running.is_some() || !self.queue.is_empty()
    }
    pub fn start(&mut self, wake: Arc<dyn Fn() + Send + Sync>) {
        self.wake = Some(wake);
        if self.enabled {
            self.refresh();
        }
    }
    pub fn status_known(&self) -> bool {
        self.known
    }
    pub fn message(&self) -> Option<(String, bool)> {
        self.message.clone()
    }

    pub fn submit(&mut self, request: PersonalRequest) {
        if !self.enabled {
            return;
        }
        match request {
            PersonalRequest::Refresh => self.refresh(),
            PersonalRequest::Install { repo } => self.enqueue(Action::Install, repo, Vec::new()),
            PersonalRequest::Open { repo, args } => self.enqueue(Action::Open, repo, args),
            PersonalRequest::UpdateAll { repos } => {
                for repo in repos {
                    self.enqueue(Action::Install, repo, Vec::new());
                }
            }
        }
        self.start_next();
    }

    pub fn ui_rows(&self) -> Vec<PersonalApp> {
        let ids: HashMap<_, _> = catalog_rows().into_iter().map(|r| (r.repo, r.id)).collect();
        self.rows
            .values()
            .filter_map(|r| {
                Some(PersonalApp {
                    id: ids.get(&r.repo)?.clone(),
                    repo: r.repo.clone(),
                    state: match r.state {
                        ForkState::New => PersonalState::New,
                        ForkState::NeedsClone => PersonalState::NeedsClone,
                        ForkState::NeedsBranch => PersonalState::NeedsBranch,
                        ForkState::Ready => PersonalState::Ready,
                        ForkState::Error => PersonalState::Error,
                    },
                    remote_sha: r.remote_sha.clone(),
                    installed_sha: r.installed_sha.clone(),
                    installed_msi_version: r.installed_msi_version.clone(),
                    detail: r.detail.clone(),
                    build_supported: r.build_supported,
                })
            })
            .collect()
    }

    pub fn tick(&mut self) {
        let result = match self.running.as_ref().map(|w| w.receiver.try_recv()) {
            Some(Ok(v)) => Some(v),
            Some(Err(TryRecvError::Disconnected)) => Some(Err("helper worker disconnected".to_string())),
            Some(Err(TryRecvError::Empty)) | None => None,
        };
        let Some(result) = result else { return };
        let Some(work) = self.running.take() else { return };
        match result {
            Ok(output) if work.action == Action::Status => match status_from_output(output, &work.expected) {
                Ok(rows) => {
                    self.rows = rows.into_iter().map(|r| (r.repo.clone(), r)).collect();
                    self.known = true;
                    if self.message.as_ref().is_some_and(|(_, error)| *error) {
                        self.message = None;
                    }
                }
                Err(e) => {
                    self.rows.clear();
                    self.known = false;
                    self.fail(e);
                }
            },
            Ok(output) if output.status.success() => {
                let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
                let launcher_installed = work.action == Action::Install && work.expected.iter().any(|repo| repo == "craft-launcher");
                let text = if launcher_installed {
                    "Launcher update installed. Quit ArtCraft Launcher completely and reopen it to use the new build.".to_string()
                } else if text.is_empty() {
                    format!("{} completed", work.action.argument())
                } else {
                    text
                };
                self.message = Some((text, false));
                if work.action.mutates() {
                    self.refresh_when_idle = true;
                }
            }
            Ok(output) => self.fail(output_error(&output)),
            Err(e) => self.fail(e),
        }
        self.start_next();
        if self.running.is_none() && self.queue.is_empty() && self.refresh_when_idle {
            self.refresh_when_idle = false;
            self.refresh();
        }
    }

    fn fail(&mut self, error: String) {
        self.message = Some((format!("{error} See the launcher log for details."), true));
    }
    fn refresh(&mut self) {
        if self.running.is_some() || !self.queue.is_empty() {
            self.refresh_when_idle = true;
            return;
        }
        let repos = catalog_rows().into_iter().map(|r| r.repo).collect::<Vec<_>>();
        self.spawn(Action::Status, repos, Vec::new());
    }
    fn enqueue(&mut self, action: Action, repo: String, args: Vec<String>) {
        let catalog: HashSet<_> = catalog_rows().into_iter().map(|r| r.repo).collect();
        if !self.known || !catalog.contains(&repo) {
            self.fail("Status is unavailable; refresh before changing a fork.".to_string());
            return;
        }
        let allowed = self.rows.get(&repo).is_some_and(|r| match action {
            Action::Install => r.build_supported && r.state != ForkState::Error,
            Action::Open => r.is_installed(),
            Action::Status => false,
        });
        if allowed {
            self.queue.push_back((action, repo, args));
        } else {
            self.fail("The verified helper state does not allow that action.".to_string());
        }
    }
    fn start_next(&mut self) {
        if self.running.is_some() {
            return;
        }
        if let Some((action, repo, args)) = self.queue.pop_front() {
            self.spawn(action, vec![repo], args);
        }
    }
    fn spawn(&mut self, action: Action, expected: Vec<String>, args: Vec<String>) {
        let (sender, receiver) = channel();
        let (python, helper, root, wake, repos) = (self.python.clone(), self.helper.clone(), self.root.clone(), self.wake.clone(), expected.clone());
        std::thread::spawn(move || {
            let result = invoke(&python, &helper, &root, action, &repos, &args);
            let _ = sender.send(result);
            if let Some(wake) = wake {
                wake();
            }
        });
        self.running = Some(Work { action, expected, receiver });
    }
}

struct CatalogRow {
    id: String,
    repo: String,
}
fn catalog_rows() -> Vec<CatalogRow> {
    let mut rows: Vec<_> = artcraft_launcher_core::hosted_catalog::apps()
        .iter()
        .filter(|app| app.kind == AppKind::Official)
        .filter_map(|app| app.repo.strip_prefix("storytold/").map(|repo| CatalogRow { id: app.id.to_string(), repo: repo.to_string() }))
        .collect();
    rows.push(CatalogRow { id: artcraft_launcher_core::catalog::LAUNCHER_ID.to_string(), repo: "craft-launcher".to_string() });
    rows
}

fn invoke(python: &Path, helper: &Path, root: &Path, action: Action, repos: &[String], forwarded: &[String]) -> Result<Output, String> {
    use std::os::windows::process::CommandExt;
    let mut command = Command::new(python);
    command.env("PYTHONUTF8", "1").arg("-B").arg(helper).arg("--root").arg(root).arg(action.argument()).args(repos);
    if action == Action::Open && !forwarded.is_empty() {
        command.arg("--").args(forwarded);
    }
    command.creation_flags(CREATE_NO_WINDOW).output().map_err(|e| format!("Could not run {}: {e}", helper.display()))
}

fn status_from_output(output: Output, expected: &[String]) -> Result<Vec<StatusRecord>, String> {
    if !output.status.success() {
        return Err(output_error(&output));
    }
    let rows: Vec<StatusRecord> = serde_json::from_slice(&output.stdout).map_err(|e| format!("Malformed helper status JSON: {e}"))?;
    let expected: HashSet<&str> = expected.iter().map(String::as_str).collect();
    let mut seen = HashSet::new();
    for row in &rows {
        if !expected.contains(row.repo.as_str()) || !seen.insert(row.repo.as_str()) {
            return Err(format!("Helper returned an unknown or duplicate repository: {}", row.repo));
        }
        if row.remote_sha.as_deref().is_some_and(str::is_empty)
            || row.installed_sha.as_deref().is_some_and(str::is_empty)
            || row.installed_msi_version.as_deref().is_some_and(str::is_empty)
        {
            return Err(format!("Helper returned an invalid commit status for {}", row.repo));
        }
        if row.state == ForkState::Ready && row.remote_sha.is_none() {
            return Err(format!("Helper omitted the live chey commit for {}", row.repo));
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::windows::process::ExitStatusExt;
    use std::process::ExitStatus;
    fn output(stdout: &str) -> Output {
        Output { status: ExitStatus::from_raw(0), stdout: stdout.as_bytes().to_vec(), stderr: Vec::new() }
    }
    #[test]
    fn new_update_and_open_states_remain_distinct() -> Result<(), String> {
        let expected = vec!["a".into(), "b".into(), "c".into()];
        let json = r#"[{"repo":"a","state":"new","remote_sha":null,"installed_sha":null,"detail":"","build_supported":true},{"repo":"b","state":"ready","remote_sha":"2222222222222222","installed_sha":null,"installed_msi_version":"1.4.0","detail":"","build_supported":true},{"repo":"c","state":"ready","remote_sha":"3333333333333333","installed_sha":"3333333333333333","detail":"","build_supported":true}]"#;
        let rows = status_from_output(output(json), &expected)?;
        assert_eq!(rows[0].state, ForkState::New);
        assert_ne!(rows[1].remote_sha, rows[1].installed_sha);
        assert!(rows[1].is_installed());
        assert_eq!(rows[1].installed_msi_version.as_deref(), Some("1.4.0"));
        assert_eq!(rows[2].remote_sha, rows[2].installed_sha);
        Ok(())
    }
    #[test]
    fn invalid_or_omitted_rows_fail_closed() {
        assert!(
            status_from_output(output(r#"[{"repo":"wrong","state":"new","remote_sha":null,"installed_sha":null,"detail":""}]"#), &["right".into()])
                .is_err()
        );
    }
}
