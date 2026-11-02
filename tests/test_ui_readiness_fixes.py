import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import db, traceability_dashboard as dashboard


class FreshDashboardTests(unittest.TestCase):
    def summary(self, statuses):
        requirements = [dict(id=str(i), doc_id='d', text='criterion', status=status,
                             ai_status=status, covers=['fn'], ai_covers=['fn'], run_id=run)
                        for i, (status, run) in enumerate(statuses)]
        return dashboard.compute_summary([dict(id='d', name='Story')], requirements, [],
                                         [], {}, latest_run_id='new')

    def test_each_historical_status_is_reassessment_not_a_current_conclusion(self):
        for status in ('supported', 'contradicted', 'unknown', 'needs_human_input'):
            with self.subTest(status=status):
                summary = self.summary([(status, 'old')])
                story = summary['document_rollup'][0]
                self.assertEqual(story['health'], 'needs_reassessment')
                self.assertEqual(story['ai_health'], 'needs_reassessment')
                self.assertEqual(story['stale_count'], 1)

    def test_fresh_conflict_stays_visible_alongside_stale_evidence(self):
        summary = self.summary([('contradicted', 'new'), ('supported', 'old')])
        self.assertEqual(summary['document_rollup'][0]['health'], 'broken')
        self.assertFalse(summary['gaps']['contradicted_requirements'][0]['is_stale'])

    def test_old_conflict_does_not_override_fresh_support(self):
        summary = self.summary([('supported', 'new'), ('contradicted', 'old')])
        self.assertEqual(summary['document_rollup'][0]['health'], 'needs_reassessment')
        self.assertTrue(summary['gaps']['contradicted_requirements'][0]['is_stale'])

    def test_only_positive_graph_scores_establish_relative_centrality(self):
        self.assertEqual(dashboard._critical_modules([
            dict(module='missing'), dict(module='zero', pagerank=0),
            dict(module='positive', pagerank=.2)]), {'positive'})


class EmptyGapAssessmentTests(unittest.TestCase):
    def test_empty_saved_comparison_is_distinct_from_never_assessed(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DB_PATH', Path(directory) / 'db.sqlite'):
            db.init_db()
            # Use the database's public repository creation API shape.
            with db.get_conn() as connection:
                connection.execute("INSERT INTO repos (id,name,source_type,created_at) VALUES ('r','Repo','local','now')")
            self.assertIsNone(db.get_repo_gap_assessment('r'))
            db.replace_repo_gap_findings('r', None, ['Story'], [])
            self.assertEqual(db.list_repo_gap_findings('r'), [])
            self.assertIsNotNone(db.get_repo_gap_assessment('r')['assessed_at'])


if __name__ == '__main__':
    unittest.main()
