import os
import tempfile
import ast
import uuid
import pickle
import multiprocessing
import time
import subprocess

from log import logger
from pydriller import RepositoryMining
from pydriller.domain.commit import ModificationType

import settings
import changegraph


class GitAnalyzer:
    STORAGE_DIR = settings.get('change_graphs_storage_dir')
    GIT_REPOSITORIES_DIR = settings.get('git_repositories_dir')
    STORE_INTERVAL = settings.get('store_interval', 300)

    def build_change_graphs(self):
        repo_names = [name for name in os.listdir(self.GIT_REPOSITORIES_DIR) if not name.startswith('_')]
        with multiprocessing.Pool(processes=multiprocessing.cpu_count(), maxtasksperchild=1000) as pool:
            for repo_num, repo_name in enumerate(repo_names):
                logger.warning(f'Looking at repo {repo_name} [{repo_num+1}/{len(repo_names)}]')

                commits = self._extract_commits(repo_name)

                logger.info(f'Pool started computations')
                pool.map(self._get_commit_change_graphs, commits)
                logger.info('Pool stopped')

    def _extract_commits(self, repo_name):
        start = time.time()

        repo_path = os.path.join(self.GIT_REPOSITORIES_DIR, repo_name)
        repo_url = self._get_repo_url(repo_path)
        repo = RepositoryMining(repo_path)

        commits = []
        for commit in repo.traverse_commits():
            if not commit.parents:
                continue

            cut = {
                'num': len(commits)+1,
                'hash': commit.hash,
                'msg': commit.msg,
                'modifications': [],
                'repo': {
                    'name': repo_name,
                    'path': repo_path,
                    'url': repo_url
                }
            }

            for mod in commit.modifications:
                cut['modifications'].append({
                    'type': mod.change_type,

                    'old_src': mod.source_code_before,
                    'old_path': mod.old_path,

                    'new_src': mod.source_code,
                    'new_path': mod.new_path
                })

            commits.append(cut)

        logger.log(logger.WARNING, 'Commits extracted', start_time=start)
        return commits

    @staticmethod
    def _get_repo_url(repo_path):
        args = ['git', 'config', '--get', 'remote.origin.url']
        result = subprocess.run(args, stdout=subprocess.PIPE, cwd=repo_path).stdout.decode('utf-8')
        return result.strip()

    @staticmethod
    def _store_change_graphs(graphs):
        filename = uuid.uuid4().hex
        logger.log(logger.INFO, f'Storing graphs to {filename}', show_pid=True)

        with open(os.path.join(GitAnalyzer.STORAGE_DIR, f'{filename}.pickle'), 'w+b') as f:
            pickle.dump(graphs, f)

        logger.log(logger.INFO, f'Storing graphs to {filename} finished', show_pid=True)

    @staticmethod
    def _get_commit_change_graphs(commit):
        change_graphs = []
        commit_msg = commit['msg'].replace('\n', '; ')
        logger.log(logger.INFO, f'Looking at commit #{commit["hash"]}, msg: "{commit_msg}"', show_pid=True)

        for mod in commit['modifications']:
            if mod['type'] != ModificationType.MODIFY:
                continue

            if not all([mod['old_path'].endswith('.py'), mod['new_path'].endswith('.py')]):
                continue

            old_method_to_new = GitAnalyzer._get_methods_mapping(
                GitAnalyzer._extract_methods(mod['old_path'], mod['old_src']),
                GitAnalyzer._extract_methods(mod['new_path'], mod['new_src'])
            )

            for old_method, new_method in old_method_to_new.items():
                old_method_src = old_method.get_source()
                new_method_src = new_method.get_source()

                if not all([old_method_src, new_method_src]) or old_method_src == new_method_src:
                    continue

                with tempfile.NamedTemporaryFile(mode='w+t', suffix='.py') as t1, \
                        tempfile.NamedTemporaryFile(mode='w+t', suffix='.py') as t2:

                    t1.writelines(old_method_src)
                    t1.seek(0)
                    t2.writelines(new_method_src)
                    t2.seek(0)

                    repo_info = RepoInfo(
                        commit['repo']['name'],
                        commit['repo']['path'],
                        commit['repo']['url'],
                        commit['hash'],
                        old_method,
                        new_method
                    )

                    try:
                        cg = changegraph.build_from_files(
                            os.path.realpath(t1.name), os.path.realpath(t2.name), repo_info=repo_info)
                    except:
                        logger.log(logger.ERROR,
                                   f'Unable to build a change graph for '
                                   f'repo={commit["repo"]["path"]}, '
                                   f'commit=#{commit["hash"]}, '
                                   f'method={old_method.full_name}, '
                                   f'line={old_method.ast.lineno}', exc_info=True, show_pid=True)
                        continue

                    change_graphs.append(cg)

                    if len(change_graphs) >= GitAnalyzer.STORE_INTERVAL:
                        GitAnalyzer._store_change_graphs(change_graphs)
                        change_graphs.clear()

        if change_graphs:
            GitAnalyzer._store_change_graphs(change_graphs)

    @staticmethod
    def _extract_methods(file_path, src):
        try:
            src_ast = ast.parse(src, mode='exec')
        except:
            logger.log(logger.INFO, 'Unable to compile src and extract methods', exc_info=True, show_pid=True)
            return []

        return ASTMethodExtractor(file_path, src).visit(src_ast)

    @staticmethod
    def _get_methods_mapping(old_methods, new_methods):
        old_method_to_new = {}
        for old_method in old_methods:
            for new_method in new_methods:
                if old_method.full_name == new_method.full_name:
                    old_method_to_new[old_method] = new_method
        return old_method_to_new


class ASTMethodExtractor(ast.NodeVisitor):
    def __init__(self, path, src):
        self.file_path = path
        self.src = src

    def visit_Module(self, node):
        methods = []
        for st in node.body:
            result = self.visit(st)
            if result:
                methods += result
        return methods

    def visit_ClassDef(self, node):
        methods = []
        for st in node.body:
            result = self.visit(st)
            if result:
                methods += result

        for method in methods:
            method.extend_path(node.name)

        return methods

    def visit_FunctionDef(self, node):
        return [Method(self.file_path, node.name, node, self.src)]


class Method:
    def __init__(self, path, name, ast, src):
        self.file_path = path
        self.ast = ast
        self.src = src.strip()

        self.name = name
        self.full_name = name

    def extend_path(self, prefix, separator='.'):
        self.full_name = f'{prefix}{separator}{self.full_name}'

    # TODO:  last = lines[end_lineno].encode()[:end_col_offset].decode(), IndexError: list index out of range
    def get_source(self):
        try:
            return ast.get_source_segment(self.src, self.ast)
        except:
            logger.info(f'Unable to extract source segment from {self.ast}', show_pid=True)
            return None


class RepoInfo:
    def __init__(self, repo_name, repo_path, repo_url, commit_hash, old_method, new_method):
        self.repo_name = repo_name
        self.repo_path = repo_path
        self.repo_url = repo_url

        self.commit_hash = commit_hash

        self.old_method = old_method
        self.new_method = new_method