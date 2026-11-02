<template>
  <div class="page">
    <el-result icon="warning" title="无权访问" :sub-title="`${auth.me?.name ?? ''}（${auth.me?.role_name ?? ''}）没有访问该页面的权限，如需开通请联系系统管理员。`">
      <template #extra><el-button type="primary" @click="router.replace('/overview')">返回总览</el-button></template>
    </el-result>
  </div>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useAuth } from '@/stores/auth'

const auth = useAuth()
const router = useRouter()
</script>
