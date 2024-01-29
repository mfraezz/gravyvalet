from .interfaces import (
    BaseAddonInterface,
    PagedResult,
)
from .operation import (
    immediate_operation,
    proxy_act_operation,
    proxy_read_operation,
)


# what a base StorageInterface could be like (incomplete)
class StorageInterface(BaseAddonInterface):
    ##
    # "item-read" operations:

    @immediate_operation
    def item_download_url(self, item_id: str) -> str:
        raise NotImplementedError  # e.g. waterbutler url, when appropriate

    @proxy_read_operation
    async def get_item_description(self, item_id: str) -> dict:
        raise NotImplementedError

    ##
    # "item-write" operations:

    @immediate_operation
    def item_upload_url(self, item_id: str) -> str:
        raise NotImplementedError

    @proxy_act_operation
    async def pls_delete_item(self, item_id: str):
        raise NotImplementedError

    ##
    # "tree-read" operations:

    @proxy_read_operation
    async def get_root_item_ids(self) -> PagedResult[str]:
        raise NotImplementedError

    @proxy_read_operation
    async def get_parent_item_id(self, item_id: str) -> str | None:
        raise NotImplementedError

    @proxy_read_operation
    async def get_item_path(self, item_id: str) -> str:
        raise NotImplementedError

    @proxy_read_operation
    async def get_child_item_ids(self, item_id: str) -> PagedResult[str]:
        raise NotImplementedError

    ##
    # "tree-write" operations

    @proxy_act_operation
    async def pls_move_item(self, item_id: str, new_treepath: str):
        raise NotImplementedError

    @proxy_act_operation
    async def pls_copy_item(self, item_id: str, new_treepath: str):
        raise NotImplementedError

    ##
    # "version-read" operations

    @proxy_read_operation
    async def get_current_version_id(self, item_id: str) -> str:
        raise NotImplementedError

    @proxy_read_operation
    async def get_version_ids(self, item_id: str) -> PagedResult[str]:
        raise NotImplementedError

    ##
    # "version-write" operations

    @proxy_act_operation
    async def pls_restore_version(self, item_id: str, version_id: str):
        raise NotImplementedError
