import unittest
from unittest.mock import patch

from app import mail_preview


class MailPreviewTests(unittest.TestCase):
    def test_examples_cover_notification_types_and_use_only_reserved_addresses(self):
        events = mail_preview.examples()
        subjects = [event.subject for event in events]
        self.assertCountEqual(subjects, [
            'Nueva solicitud de clase', 'Solicitud de clase recibida',
            'Clase confirmada', 'Solicitud de clase rechazada',
            'Clase reprogramada', 'Clase reprogramada',
            'Clase cancelada', 'Clase cancelada',
            'Solicitud de clase cancelada', 'Solicitud de clase cancelada',
            'Nueva solicitud de contacto', 'Recordatorio de tu clase', 'Nuevo alumno registrado',
        ])
        self.assertTrue(all(event.recipient.endswith('@example.invalid') for event in events))

    def test_examples_use_realistic_weekday_times_and_previous_only_for_reschedule(self):
        events = mail_preview.examples()
        for event in events:
            if event.subject in ('Clase cancelada', 'Clase reprogramada'):
                self.assertEqual('Horario anterior:' in event.body, event.subject == 'Clase reprogramada')
            if event.subject not in ('Nueva solicitud de contacto', 'Nuevo alumno registrado'):
                self.assertIn('lunes 2 de noviembre de 2026', event.body)
                self.assertIn('de 14:00 a 15:00 (hora de Madrid)', event.body)

    def test_delivery_requires_fixed_isolated_smtp_settings(self):
        with patch.object(mail_preview.notifications.settings, 'smtp_host', 'external.example'):
            with self.assertRaises(RuntimeError):
                mail_preview.send_examples()
