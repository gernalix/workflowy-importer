# Preserved projection branch history

The original heads `0ab514fa4bb23c240f8d617b21d757f036169009` (human titles / Focus) and `644d0109d7aa39873acc008dffcb13a092c7777f` (order changes during membership churn) are retained as merge ancestors before retiring their source branches.

Both changes target only `src/workflowy_importer/work_items_projection.py` and its dedicated test module. The main branch deliberately removed those files in `d80cab4`, and README's C3 boundary confirms that roadmap sync and manual ordering routes are retired. The human-title patch was already patch-equivalent in main history (`git cherry` reports it integrated); its original commit is now retained as well.

The modify/delete resolutions preserve that explicit retirement. They do not recreate a projection, revive C2/C3 lifecycle, or change the personal capture/import, ChatGPT import or external-system adapters. The original implementation and regression tests remain available through the merge parents and the private source preservation performed by the purification command.
