//! Native-shell supplied state for source builds. The web build never enables this adapter.

use std::collections::HashMap;

/// Verified helper state for one official catalog app.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PersonalState {
    New,
    NeedsClone,
    NeedsBranch,
    Ready,
    Error,
}

/// The small, meaningful subset of a helper status row used by the launcher UI.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PersonalApp {
    pub id: String,
    pub repo: String,
    pub state: PersonalState,
    pub remote_sha: Option<String>,
    pub installed_sha: Option<String>,
    pub installed_msi_version: Option<String>,
    pub detail: String,
    pub build_supported: bool,
}

impl PersonalApp {
    /// The helper found either an exact managed build or one installed Windows Installer product.
    pub fn is_installed(&self) -> bool {
        self.installed_sha.is_some() || self.installed_msi_version.is_some()
    }

    /// This installed source can be brought to the helper's known current build.
    pub(crate) fn update_available(&self) -> bool {
        if !self.is_installed() {
            return false;
        }
        match self.state {
            PersonalState::NeedsClone | PersonalState::NeedsBranch => true,
            PersonalState::Ready => self.remote_sha.is_some() && self.remote_sha != self.installed_sha,
            PersonalState::New | PersonalState::Error => false,
        }
    }
}

/// A request returned to the native shell. No helper or filesystem work happens in this crate.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PersonalRequest {
    Refresh,
    Install { repo: String },
    Open { repo: String, args: Vec<String> },
    UpdateAll { repos: Vec<String> },
}

#[derive(Default)]
pub(crate) struct PersonalMode {
    rows: HashMap<String, PersonalApp>,
    launcher: Option<PersonalApp>,
    pub busy: bool,
    pub status_known: bool,
    pub message: Option<(String, bool)>,
    pub requests: Vec<PersonalRequest>,
}

impl PersonalMode {
    pub fn replace(&mut self, rows: Vec<PersonalApp>, busy: bool, status_known: bool, message: Option<(String, bool)>) {
        self.rows.clear();
        self.launcher = None;
        for row in rows {
            if row.id == artcraft_launcher_core::catalog::LAUNCHER_ID {
                self.launcher = Some(row);
            } else {
                self.rows.insert(row.id.clone(), row);
            }
        }
        self.busy = busy;
        self.status_known = status_known;
        self.message = message;
    }

    pub fn row(&self, id: &str) -> Option<&PersonalApp> {
        self.rows.get(id)
    }

    pub fn any_row(&self, id: &str) -> Option<&PersonalApp> {
        if id == artcraft_launcher_core::catalog::LAUNCHER_ID { self.launcher.as_ref() } else { self.rows.get(id) }
    }

    pub fn launcher(&self) -> Option<&PersonalApp> {
        self.launcher.as_ref()
    }
}

pub(crate) fn short_sha(sha: &str) -> &str {
    sha.get(..12).unwrap_or(sha)
}
