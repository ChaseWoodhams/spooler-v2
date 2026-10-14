import unittest
from tools.orca_settings import object_scope


class ObjectScopeSourceTests(unittest.TestCase):
    def test_comments_and_other_config_classes_cannot_grant_object_edit_permission(self):
        source = '''
PRINT_CONFIG_CLASS_DEFINE(PrintObjectConfig,
 ((ConfigOptionFloat, layer_height))
 // ((ConfigOptionFloat, travel_speed))
 /* ((ConfigOptionString, commented_option)) */
)
PRINT_CONFIG_CLASS_DEFINE(PrintRegionConfig,
 ((ConfigOptionInt, wall_loops))
)
PRINT_CONFIG_CLASS_DERIVED_DEFINE(PrintConfig, (GCodeConfig),
 ((ConfigOptionFloat, travel_speed))
)
'''
        self.assertEqual(object_scope(source), ['layer_height', 'wall_loops'])
