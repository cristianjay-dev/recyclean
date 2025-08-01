from django.db import models

# -------------------------
# DROP-OFF SITE TABLE
# -------------------------
class DropOffSite(models.Model):
    id = models.AutoField(primary_key=True)
    barangay = models.CharField(max_length=100, unique=True)
    assigned_staff = models.OneToOneField(
        'User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='dropoff_site'
    )

    class Meta:
        managed = True
        db_table = 'dropoff_sites'

    def __str__(self):
        return f"{self.barangay} Drop-Off Site"

# -------------------------
# USER TABLE (Residents & Staff)
# -------------------------
class User(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255)
    email = models.CharField(max_length=255)
    mobile_number = models.CharField(max_length=20, unique=True)
    password_hash = models.TextField()
    user_type = models.CharField(max_length=10)  # 'resident' or 'staff'
    barangay = models.CharField(max_length=100)
    is_approved = models.BooleanField(default=False)
    date_joined = models.DateTimeField()
    account_status = models.CharField(max_length=10)  # 'active' or 'disabled'
    registered_by_admin = models.BooleanField()

    class Meta:
        managed = True
        db_table = 'users'

    def __str__(self):
        return self.name

# -------------------------
# SUBMISSIONS TABLE
# -------------------------
class Submission(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column='user_id', related_name='submissions')
    staff = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column='staff_id', related_name='processed_submissions')
    volume_type = models.CharField(max_length=10)
    quantity = models.IntegerField()
    image_path = models.TextField()
    processing_mode = models.CharField(max_length=20)
    estimated_quantity = models.IntegerField()
    confidence_score = models.FloatField()
    points_awarded = models.IntegerField()
    date_submitted = models.DateTimeField()

    class Meta:
        managed = True
        db_table = 'submissions'

# -------------------------
# REWARD REQUESTS TABLE
# -------------------------
class RewardRequest(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column='user_id')
    mobile_number = models.CharField(max_length=20)
    telco = models.CharField(max_length=10)
    points_used = models.IntegerField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10)
    processed_by = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column='processed_by', related_name='processed_rewards', null=True)
    date_requested = models.DateTimeField()
    date_processed = models.DateTimeField(null=True)

    class Meta:
        managed = True
        db_table = 'reward_requests'

    def __str__(self):
        return f"{self.user.name} - ₱{self.amount} ({self.status})"

# -------------------------
# QUIZ CATEGORY TABLE
# -------------------------
class QuizCategory(models.Model):
    id = models.AutoField(primary_key=True)
    category_name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    feedback = models.TextField(blank=True, null=True)

    class Meta:
        managed = True
        db_table = 'quiz_categories'

    def __str__(self):
        return self.category_name

# -------------------------
# QUIZ QUESTIONS TABLE
# -------------------------
class Quiz(models.Model):
    id = models.AutoField(primary_key=True)
    category = models.ForeignKey(
        QuizCategory,
        on_delete=models.DO_NOTHING,
        db_column='category_id',
        related_name='quiz_set',
        default=1
    )
    question = models.TextField()
    option_a = models.TextField()
    option_b = models.TextField()
    option_c = models.TextField()
    option_d = models.TextField()
    correct_option = models.CharField(max_length=1)
    points = models.IntegerField()
    feedback = models.TextField(blank=True, null=True)

    class Meta:
        managed = True
        db_table = 'quizzes'

    def __str__(self):
        return self.question

# -------------------------
# QUIZ ANSWERS TABLE
# -------------------------
class QuizAnswer(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column='user_id', default=1)
    quiz = models.ForeignKey(Quiz, on_delete=models.DO_NOTHING, db_column='quiz_id', default=1)
    selected_option = models.CharField(max_length=1)
    is_correct = models.BooleanField()
    date_answered = models.DateTimeField()

    class Meta:
        managed = True
        db_table = 'quiz_answers'

# -------------------------
# QUIZ SESSION TABLE
# -------------------------
class QuizSession(models.Model):
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column='user_id')
    category = models.ForeignKey(QuizCategory, on_delete=models.DO_NOTHING, db_column='category_id')
    score = models.IntegerField()
    max_score = models.IntegerField()
    bonus_awarded = models.BooleanField(default=False)
    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True
        db_table = 'quiz_sessions'

# -------------------------
# USER BADGES TABLE
# -------------------------
class UserBadge(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column='user_id', default=1)
    badge_name = models.CharField(max_length=100)
    awarded_at = models.DateTimeField()

    class Meta:
        managed = True
        db_table = 'user_badges'
