from test_like_reconsideration import LikeReconsiderationTests


class LikeMessageTests(LikeReconsiderationTests):
    def like_with_message(self, target, message, liked=True):
        return self.client.post('/api/swipes', json={
            'target_id': target, 'liked': liked, 'message': message
        })

    def test_message_is_visible_to_recipient_and_copied_on_match_once(self):
        response = self.like_with_message('bob', '  Salut Bob !  ')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()['match'])
        self.actor = 'bob'
        self.assertEqual(self.client.get('/api/likes').get_json()[0]['like_message'], 'Salut Bob !')
        response = self.like_with_message('alice', 'Bonjour Alice !')
        self.assertIsNotNone(response.get_json()['match'])
        self.assertEqual(self.db.execute('SELECT sender_id,body FROM messages ORDER BY sender_id').fetchall(), [
            {'sender_id': 'alice', 'body': 'Salut Bob !'},
            {'sender_id': 'bob', 'body': 'Bonjour Alice !'},
        ])
        self.like_with_message('alice', 'Bonjour Alice !')
        self.assertEqual(len(self.db.execute('SELECT * FROM messages').fetchall()), 2)

    def test_message_is_retained_when_accepted_without_message(self):
        self.like_with_message('bob', 'Salut !')
        self.actor = 'bob'
        self.swipe('alice', True)
        self.assertEqual(self.db.execute('SELECT body FROM messages').fetchall(), [{'body': 'Salut !'}])

    def test_message_validation(self):
        for message in ['a' * 501, 123, ['hello']]:
            self.assertEqual(self.like_with_message('bob', message).status_code, 400)
        self.assertEqual(self.like_with_message('bob', 'Hello', liked=False).status_code, 400)
        self.assertEqual(self.db.execute('SELECT * FROM swipes').fetchall(), [])
        self.assertEqual(self.like_with_message('bob', 'a' * 500).status_code, 200)

    def test_retry_does_not_replace_original_note(self):
        self.like_with_message('bob', 'Premier message')
        self.like_with_message('bob', 'Autre message')
        self.actor = 'bob'
        self.assertEqual(self.client.get('/api/likes').get_json()[0]['like_message'], 'Premier message')
