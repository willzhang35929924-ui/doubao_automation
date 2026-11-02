"""持久化审核队列的单实例处理入口。"""
import sys

if sys.platform == 'darwin':
    sys.dont_write_bytecode = True

from doubao_automation.paths import use_workspace_temp

if __name__=='__main__':
    use_workspace_temp()
    from doubao_automation.postprocess import worker_main
    worker_main()
