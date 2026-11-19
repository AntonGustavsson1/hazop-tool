"""Explicit protection links for multi-consequence LOPA fixtures."""
def protect_with_same_function(db, sensor, consequence):
    links = db.safeguard_equipment_links(sensor)
    if not links:
        equipment = db.add_equipment_item('SCOPE-1', 'SCOPE-1', 'LT', 0, '', '', 0)
        db.add_safeguard_equipment_link(sensor, equipment, 'HH')
        links = db.safeguard_equipment_links(sensor)
    barrier = db.add_safeguard(consequence)
    db.update_safeguard(barrier, description='Same protected function', rrf=100, sg_type='SIS')
    for link in links:
        db.add_safeguard_equipment_link(barrier, link['equipment_id'], link['trigger_code'], link.get('trigger_custom') or '')
    return barrier
