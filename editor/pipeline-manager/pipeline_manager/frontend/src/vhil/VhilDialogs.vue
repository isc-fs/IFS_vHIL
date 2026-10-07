<!--
vHIL: Commit…, Open PR and the keyboard map, as modal dialogs
(CHANGELOG-VHIL.md): what the shell's Save and PR forms did. Native
<dialog>: focus moves in and back, and Escape closes it.
-->

<template>
    <div class="vhil-dialogs">
    <dialog ref="commitDlg" class="vhil-dialog" aria-labelledby="vhil-commit-title" @close="closed">
        <form class="vhil-form" @submit.prevent="doCommit">
            <h2 id="vhil-commit-title" class="vhil-panel-title">Commit {{ ws.id }}</h2>
            <p class="muted">
                Writes systems/{{ ws.id }}.yaml from the graph and commits it to a branch of the
                workspace. Run then runs that commit.
            </p>
            <label>Branch
                <input
                    v-model="ws.commit.branch" class="vhil-input mono" required placeholder="feat/…"
                />
            </label>
            <label>Message
                <textarea v-model="ws.commit.message" class="vhil-input" rows="3" required />
            </label>
            <p v-if="ws.problems.length" class="vhil-note">
                {{ errors }} error(s), {{ warnings }} warning(s) at the last Check: see Problems.
            </p>
            <div class="vhil-actions">
                <button type="button" class="vhil-btn" :disabled="ws.busy" @click="check">
                    Check
                </button>
                <span class="vhil-spacer" />
                <button type="button" class="vhil-btn" @click="close">Cancel</button>
                <button type="submit" class="vhil-btn --primary" :disabled="ws.busy">
                    Commit to branch
                </button>
            </div>
        </form>
    </dialog>

    <dialog ref="prDlg" class="vhil-dialog" aria-labelledby="vhil-pr-title" @close="closed">
        <form class="vhil-form" @submit.prevent="doPr">
            <h2 id="vhil-pr-title" class="vhil-panel-title">
                Pull request to {{ ws.config.base_branch }}
            </h2>
            <p class="muted">
                From <span class="mono">{{ prBranch }}</span>.
            </p>
            <label>Title
                <input v-model="ws.pr.title" class="vhil-input" required />
            </label>
            <label>Body
                <textarea v-model="ws.pr.body" class="vhil-input" rows="4" />
            </label>
            <p v-if="!ws.config.can_open_pr" class="vhil-note">
                No GitHub credentials on the server (VHIL_GITHUB_TOKEN or the GitHub App):
                committed branches stay in the workspace. Push and open the PR by hand.
            </p>
            <p v-if="prUrl" class="vhil-note">
                Opened <a :href="prUrl" target="_blank" rel="noopener">{{ prUrl }}</a>
            </p>
            <div class="vhil-actions">
                <span class="vhil-spacer" />
                <button type="button" class="vhil-btn" @click="close">Close</button>
                <button
                    type="submit" class="vhil-btn --primary"
                    :disabled="ws.busy || !ws.config.can_open_pr"
                >
                    Open PR
                </button>
            </div>
        </form>
    </dialog>

    <dialog ref="keysDlg" class="vhil-dialog" aria-labelledby="vhil-keys-title" @close="closed">
        <h2 id="vhil-keys-title" class="vhil-panel-title">Keyboard shortcuts</h2>
        <table class="vhil-keys">
            <tbody>
                <tr v-for="[keys, what] in SHORTCUTS" :key="keys">
                    <td><kbd>{{ keys }}</kbd></td><td>{{ what }}</td>
                </tr>
            </tbody>
        </table>
        <div class="vhil-actions">
            <span class="vhil-spacer" />
            <button type="button" class="vhil-btn" @click="close">Close</button>
        </div>
    </dialog>
    </div>
</template>

<script>
import {
    computed, defineComponent, ref, watch,
} from 'vue';
import {
    ws, check, commit, openPr, problemCount,
} from './workspace.js';
import { SHORTCUTS } from './shortcuts.js';

export default defineComponent({
    setup() {
        const commitDlg = ref(null);
        const prDlg = ref(null);
        const keysDlg = ref(null);
        const prUrl = ref('');
        const dialogs = { commit: commitDlg, pr: prDlg, shortcuts: keysDlg };

        watch(() => ws.dialog, (name) => {
            Object.entries(dialogs).forEach(([key, dlg]) => {
                if (key === name && !dlg.value.open) dlg.value.showModal();
                else if (key !== name && dlg.value?.open) dlg.value.close();
            });
            if (name === 'pr') prUrl.value = '';
        });
        const close = () => { ws.dialog = null; };
        const closed = () => {
            if (!Object.values(dialogs).some((d) => d.value?.open)) ws.dialog = null;
        };

        const doCommit = async () => {
            const out = await commit();
            if (out) close();
        };
        const doPr = async () => {
            const out = await openPr();
            if (out) prUrl.value = out.url;
        };

        return {
            ws,
            commitDlg,
            prDlg,
            keysDlg,
            prUrl,
            close,
            closed,
            check,
            doCommit,
            doPr,
            SHORTCUTS,
            prBranch: computed(() => ws.savedBranch || ws.commit.branch || 'the committed branch'),
            errors: computed(() => problemCount('error')),
            warnings: computed(() => problemCount('warning')),
        };
    },
});
</script>
