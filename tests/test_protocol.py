import unittest

from agent_collab import protocol as p

H = 'a' * 64


def f(path, sha=H, size=1, executable=False):
    return {'path': path, 'sha256': sha, 'size': size, 'executable': executable}


class ManifestTest(unittest.TestCase):
    def test_canonical_hash_ignores_order(self):
        a = {'version': 1, 'base': None, 'files': [f('b'), f('a')], 'deleted': []}
        b = {'version': 1, 'base': None, 'files': [f('a'), f('b')], 'deleted': []}
        self.assertEqual(p.manifest_sha256(a), p.manifest_sha256(b))

    def test_rejects_unsafe_paths(self):
        for bad in ('/abs', 'C:/x', 'a\\b', 'a/../b', 'a//b', './a', ''):
            with self.assertRaises(ValueError, msg=bad):
                p.validate_manifest({'version': 1, 'base': None, 'files': [f(bad)], 'deleted': []})

    def test_rejects_file_dir_clash_and_dupes(self):
        with self.assertRaises(ValueError):
            p.validate_manifest({'version': 1, 'base': None, 'files': [f('a'), f('a/b')], 'deleted': []})
        with self.assertRaises(ValueError):
            p.validate_manifest({'version': 1, 'base': None, 'files': [f('a'), f('a')], 'deleted': []})

    def test_deleted_requires_base(self):
        with self.assertRaises(ValueError):
            p.validate_manifest({'version': 1, 'base': None, 'files': [], 'deleted': ['x']})
        p.validate_manifest({'version': 1, 'base': H, 'files': [], 'deleted': ['x']})

    def test_bool_is_not_size(self):
        with self.assertRaises(ValueError):
            p.validate_manifest({'version': 1, 'base': None, 'files': [f('a', size=True)], 'deleted': []})

    def test_case_collisions_and_diff(self):
        self.assertEqual(p.case_collisions(['A.txt', 'a.txt', 'b']), [['A.txt', 'a.txt']])
        old = p.validate_manifest({'version': 1, 'base': None, 'files': [f('a'), f('b')], 'deleted': []})
        new = p.validate_manifest({'version': 1, 'base': None, 'files': [f('a', 'b' * 64), f('c')], 'deleted': []})
        self.assertEqual(p.manifest_diff(old, new), {'added': ['c'], 'modified': ['a'], 'deleted': ['b']})

    def test_uuid(self):
        p.check_uuid('5cb86daf-bcf1-44c2-9a68-8849447476e1')
        with self.assertRaises(ValueError):
            p.check_uuid('5CB86DAF-BCF1-44C2-9A68-8849447476E1')


if __name__ == '__main__':
    unittest.main()
