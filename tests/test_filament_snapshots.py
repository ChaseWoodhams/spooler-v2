import copy
import json
import unittest

import jobs
import projects
import slicer
from tests import test_projects


class ProjectFilamentSnapshots(unittest.IsolatedAsyncioTestCase):
    setUp = test_projects.ProjectTests.setUp
    tearDown = test_projects.ProjectTests.tearDown
    asyncTearDown = test_projects.ProjectTests.asyncTearDown
    run_worker = test_projects.ProjectTests.run_worker
    project_with_cube = test_projects.ProjectTests.project_with_cube

    def material(self, name, material, temperature):
        return slicer.save_user_preset('filament', name, 'Elegoo PLA @ECC',
                                      {'filament_type': [material], 'nozzle_temperature': [str(temperature)]})

    def save(self, project, choice):
        data = copy.deepcopy(project['data'])
        data['filaments'] = {'1': choice}
        return projects.update_project(project['id'], {'data': data, 'revision': project['revision']})

    async def test_saved_alternate_content_survives_edit_delete_and_reopen(self):
        self.material('Workshop ASA', 'ASA', 260)
        project = await self.project_with_cube()
        project = self.save(project, {'preset': 'Workshop ASA', 'colour': 'black'})
        snapshot = project['data']['filaments']['1']['snapshot']
        self.assertEqual(snapshot['material'], ['ASA'])
        self.assertEqual(snapshot['version'], 1)
        self.assertEqual(len(snapshot['sha256']), 64)
        self.material('Workshop ASA', 'PETG', 240)
        slicer.delete_user_preset('filament', 'Workshop ASA')
        project = projects.get_project(project['id'])
        project = self.save(project, project['data']['filaments']['1'])
        review = projects.effective_settings(project['id'], 1)
        self.assertEqual(review['filament']['filament_type'], ['ASA'])
        self.assertEqual(review['filament']['nozzle_temperature'], ['260'])
        self.assertEqual(review['filament_choice']['snapshot'], snapshot)


    async def test_unresolved_legacy_choice_is_preserved_and_blocks_new_slice(self):
        import database
        project = await self.project_with_cube()
        data = copy.deepcopy(project['data'])
        data['filaments'] = {'1': {'preset': 'Deleted ASA', 'colour': 'black'}}
        with database.transaction() as conn:
            conn.execute('UPDATE projects SET data = ? WHERE id = ?', (json.dumps(data), project['id']))
        project = projects.get_project(project['id'])
        before = copy.deepcopy(project)
        review = projects.effective_settings(project['id'], 1)
        self.assertEqual(review['filament'], {})
        self.assertEqual(review['issues'][0]['code'], 'unresolved-filament')
        self.assertEqual(projects.get_project(project['id'])['data'], before['data'])
        project = self.save(project, data['filaments']['1'])
        self.assertEqual(project['data']['filaments']['1']['preset'], 'Deleted ASA')
        with self.assertRaisesRegex(jobs.JobError, 'Deleted ASA'):
            projects.request_slice(project['id'], 1)
        self.assertEqual(projects.get_project(project['id'])['slices'], {})

    async def test_two_plates_keep_actual_material_and_default_only_tuning(self):
        import io
        from tests.test_slicer import cube_stl, fake_runner
        from unittest import mock
        self.material('Workshop PETG', 'PETG', 245)
        self.material('Workshop ASA', 'ASA', 260)
        project = await self.project_with_cube()
        mesh = cube_stl(20)
        projects.import_model(project['id'], io.BytesIO(mesh), len(mesh), 'second.stl')
        await self.run_worker()
        project = projects.get_project(project['id'])
        data = copy.deepcopy(project['data'])
        bundle = slicer.create_bundle(slicer.DEFAULT_PRESETS['machine'], slicer.DEFAULT_PRESETS['process'], 'Workshop PETG')
        data['profile'].update(bundle_id=bundle['id'], filament={'nozzle_temperature': ['250']})
        data['plates'].append({'id': 2, 'name': 'ASA Plate'})
        data['objects'][1]['plate'] = 2
        data['filaments'] = {'2': {'preset': 'Workshop ASA', 'colour': 'PETG blue'}}
        project = projects.update_project(project['id'], {'data': data, 'revision': project['revision']})
        petg, asa = (projects.effective_settings(project['id'], n) for n in (1, 2))
        self.assertEqual((petg['filament']['filament_type'], petg['filament']['nozzle_temperature']), (['PETG'], ['250']))
        self.assertEqual((asa['filament']['filament_type'], asa['filament']['nozzle_temperature']), (['ASA'], ['260']))
        self.assertEqual(asa['filament_choice']['colour'], 'PETG blue')
        projects.request_slices(project['id'])
        with mock.patch.object(slicer, 'runner', fake_runner()):
            await self.run_worker()
        self.assertEqual([s['state'] for s in projects.get_project(project['id'])['slices'].values()], ['sliced', 'sliced'])
        for n, expected in ((1, ['PETG']), (2, ['ASA'])):
            used = json.loads((projects.plate_dir(project['id'], n) / 'profile' / 'filament.json').read_text())
            self.assertEqual(used['filament_type'], expected)

    async def test_incompatible_and_unknown_new_choices_rejected_before_save(self):
        project = await self.project_with_cube()
        for name in ('No such ASA', 'Elegoo PETG @ECC2'):
            with self.subTest(name=name), self.assertRaises(jobs.JobError):
                self.save(project, {'preset': name})
            self.assertEqual(projects.get_project(project['id'])['data'], project['data'])

    async def test_mixed_choice_issue_identifies_the_objects(self):
        self.material('Workshop ASA', 'ASA', 260)
        project = await self.project_with_cube()
        data = copy.deepcopy(project['data'])
        data['objects'][0]['name'] = 'ASA enclosure'
        # Another Model is needed: Copies share their Model choice.
        import io
        from tests.test_slicer import cube_stl
        mesh = cube_stl(10)
        projects.import_model(project['id'], io.BytesIO(mesh), len(mesh), 'bracket.stl')
        await self.run_worker()
        project = projects.get_project(project['id'])
        data = copy.deepcopy(project['data'])
        data['objects'][0]['name'] = 'ASA enclosure'
        data['objects'][1]['name'] = 'PLA bracket'
        data['filaments'] = {'1': {'preset': 'Workshop ASA'}}
        project = projects.update_project(project['id'], {'data': data, 'revision': project['revision']})
        issue = projects.effective_settings(project['id'], 1)['issues'][0]
        self.assertEqual(issue['affected_names'], ['ASA enclosure', 'PLA bracket'])
        with self.assertRaises(jobs.JobError) as error:
            projects.request_slice(project['id'], 1)
        self.assertEqual(error.exception.issues[0]['affected_names'], ['ASA enclosure', 'PLA bracket'])
    async def test_intake_discloses_actual_suggested_default_material(self):
        project = await self.project_with_cube()
        suggested = projects.intake_files(project['id'])['suggested_filament']
        self.assertEqual(suggested['material'], ['PLA'])
        self.assertEqual(suggested['name'], 'Elegoo PLA @ECC')
    async def test_saved_snapshot_damage_blocks_slice_without_erasing_history(self):
        self.material('Workshop ASA', 'ASA', 260)
        project = self.save(await self.project_with_cube(), {'preset': 'Workshop ASA'})
        snapshot = project['data']['filaments']['1']['snapshot']
        path = slicer.profiles_dir() / 'filaments' / snapshot['sha256'] / 'filament.json'
        path.write_text('{}')
        review = projects.effective_settings(project['id'], 1)
        self.assertEqual(review['filament'], {})
        self.assertIn('missing or damaged', review['issues'][0]['message'])
        project = self.save(project, project['data']['filaments']['1'])
        with self.assertRaisesRegex(jobs.JobError, 'missing or damaged'):
            projects.request_slice(project['id'], 1)
        self.assertEqual(project['data']['filaments']['1']['snapshot'], snapshot)

    async def test_profile_change_rejects_incompatible_saved_alternate(self):
        self.material('Workshop ASA', 'ASA', 260)
        project = self.save(await self.project_with_cube(), {'preset': 'Workshop ASA'})
        other = 'Elegoo Centauri Carbon 2 0.4 nozzle'
        slicer.save_user_preset('filament', 'CC2 ASA', 'Elegoo PETG @ECC2', {'filament_type': ['ASA']})
        bundle = slicer.create_bundle(other, '0.20mm Standard @Elegoo CC2 0.4 nozzle', 'CC2 ASA')
        data = copy.deepcopy(project['data'])
        data['profile']['bundle_id'] = bundle['id']
        with self.assertRaisesRegex(jobs.JobError, 'compatible'):
            projects.update_project(project['id'], {'data': data, 'revision': project['revision']})
        self.assertEqual(projects.get_project(project['id'])['data'], project['data'])

    async def test_valid_legacy_name_migrates_with_evidence_only_on_save(self):
        import database
        self.material('Workshop ASA', 'ASA', 260)
        project = await self.project_with_cube()
        data = copy.deepcopy(project['data'])
        data['filaments'] = {'1': {'preset': 'Workshop ASA', 'colour': 'black'}}
        with database.transaction() as conn:
            conn.execute('UPDATE projects SET data = ? WHERE id = ?', (json.dumps(data), project['id']))
        review = projects.effective_settings(project['id'], 1)
        self.assertEqual(review['issues'][0]['code'], 'unresolved-filament')
        self.assertNotIn('snapshot', projects.get_project(project['id'])['data']['filaments']['1'])
        project = self.save(projects.get_project(project['id']), data['filaments']['1'])
        self.assertEqual(project['data']['filaments']['1']['snapshot']['material'], ['ASA'])
        self.assertEqual(project['data']['filaments']['1']['migration']['evidence'], 'verified-catalogue-at-save')
    async def test_new_default_with_unknown_material_is_rejected(self):
        project = await self.project_with_cube()
        bundle = slicer.create_bundle('Elegoo Centauri Carbon 2 0.4 nozzle', '0.20mm Standard @Elegoo CC2 0.4 nozzle', 'Elegoo PETG @ECC2')
        data = copy.deepcopy(project['data'])
        data['profile']['bundle_id'] = bundle['id']
        with self.assertRaisesRegex(jobs.JobError, 'material identity'):
            projects.update_project(project['id'], {'data': data, 'revision': project['revision']})
        self.assertEqual(projects.get_project(project['id'])['data'], project['data'])
    async def test_default_only_legacy_errors_do_not_block_alternate_material(self):
        import database
        self.material('Workshop ASA', 'ASA', 260)
        project = self.save(await self.project_with_cube(), {'preset': 'Workshop ASA'})
        data = copy.deepcopy(project['data'])
        data['profile']['filament'] = {'nozzle_temperature': ['bad'], 'nozzle_temperature_range_low': ['300'], 'nozzle_temperature_range_high': ['200']}
        with database.transaction() as conn:
            conn.execute('UPDATE projects SET data = ? WHERE id = ?', (json.dumps(data), project['id']))
        review = projects.effective_settings(project['id'], 1)
        self.assertEqual(review['filament']['nozzle_temperature'], ['260'])
        self.assertFalse([i for i in review['issues'] if i.get('scope') == 'project' and i.get('kind') == 'filament'])
