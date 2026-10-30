import { WORKSPACE_PAGES, workspacePageFromPath, workspaceFrameUrl } from '../utils/workspace-routes.js';

// One persistent header and recording library; each existing page keeps its
// own form/media lifecycle inside the content pane rather than replacing it.
export function useWorkspace(vue, state, utils) {
    const { ref, computed, watch, onMounted, onUnmounted } = vue;
    const workspacePage = ref(workspacePageFromPath(window.location.pathname));
    const workspaceHash = ref(window.location.hash);
    const workspaceFrame = ref(null);
    const workspaceLoading = ref(workspacePage.value !== 'recordings');
    const workspacePageLabel = computed(() => WORKSPACE_PAGES[workspacePage.value].label);
    const frameUrl = computed(() => workspaceFrameUrl(workspacePage.value, workspaceHash.value));

    const writeUrl = (path, replace = false) => {
        if (window.location.pathname + window.location.search + window.location.hash !== path) {
            window.history[replace ? 'replaceState' : 'pushState']({ workspace: true }, '', path);
        }
    };

    const navigateWorkspace = (page, options = {}) => {
        if (!WORKSPACE_PAGES[page]) return false;
        if (state.currentView.value === 'recording' && (state.isRecording.value || state.audioBlobURL.value)) {
            utils.showToast('請先完成目前的錄音，再切換頁面。');
            return false;
        }
        utils.pausePlayback();
        state.showUploadModal.value = false;
        state.isUserMenuOpen.value = false;
        workspaceHash.value = options.hash || '';
        if (workspacePage.value !== page) workspaceLoading.value = page !== 'recordings';
        workspacePage.value = page;
        if (page === 'recordings') {
            state.selectedRecording.value = null;
            state.currentView.value = null;
        }
        writeUrl(page === 'recordings' ? '/?home=1' : WORKSPACE_PAGES[page].path + workspaceHash.value, options.replace);
        document.title = `${workspacePageLabel.value} · Locol-Vocol`;
        return true;
    };

    const navigateWorkspaceLink = (event, page) => {
        // Preserve native open-in-new-tab and modified-click behavior.
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        navigateWorkspace(page);
    };

    utils.onRecordingSelected = recording => {
        workspacePage.value = 'recordings';
        workspaceHash.value = '';
        workspaceLoading.value = false;
        if (recording?.id && recording.id !== 'incognito') writeUrl(`/recordings/${recording.id}`);
        else writeUrl('/?home=1');
    };

    const handleMessage = event => {
        if (event.origin !== window.location.origin || event.source !== workspaceFrame.value?.contentWindow) return;
        const data = event.data || {};
        if (data.type === 'locol:sidebar') {
            state.isSidebarCollapsed.value = !state.isSidebarCollapsed.value;
        } else if (data.type === 'locol:navigate') {
            if (Number.isInteger(data.recordingId) && data.recordingId > 0) {
                utils.selectRecording({ id: data.recordingId });
            } else if (navigateWorkspace(data.page, { hash: data.hash || '' }) && data.upload) {
                utils.switchToUploadView();
            }
        } else if (data.type === 'locol:preferences') {
            state.showTimestampsSimpleView.value = data.showTimestamps === true;
            state.editorAutosave.value = data.editorAutosave === true;
            state.audioPlayerPosition.value = data.audioPlayerPosition === 'top' ? 'top' : 'bottom';
            if (typeof data.language === 'string') window.i18n?.setLocale(data.language);
        }
    };

    const handlePopState = () => {
        const match = window.location.pathname.match(/^\/recordings\/(\d+)\/?$/);
        if (match) utils.selectRecording({ id: Number(match[1]) });
        else navigateWorkspace(workspacePageFromPath(window.location.pathname), { hash: window.location.hash, replace: true });
    };

    watch(state.currentView, view => {
        if (view === 'recording') {
            workspacePage.value = 'recordings';
            writeUrl('/?home=1');
        }
    });
    onMounted(() => {
        if (workspacePage.value !== 'recordings') document.title = `${workspacePageLabel.value} · Locol-Vocol`;
        window.addEventListener('message', handleMessage);
        window.addEventListener('popstate', handlePopState);
    });
    onUnmounted(() => {
        window.removeEventListener('message', handleMessage);
        window.removeEventListener('popstate', handlePopState);
        utils.onRecordingSelected = null;
    });

    return { workspacePage, workspaceFrame, workspaceLoading, workspacePageLabel,
        workspaceFrameUrl: frameUrl, navigateWorkspace, navigateWorkspaceLink };
}
