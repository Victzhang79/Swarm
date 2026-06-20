package com.ruoyi.alarm.mapper;

import java.util.List;
import com.ruoyi.alarm.domain.AlarmTaskChannel;

/**
 * 预警任务渠道 数据层
 *
 * @author ruoyi
 */
public interface AlarmTaskChannelMapper
{
    /**
     * 查询预警任务渠道
     *
     * @param id 预警任务渠道ID
     * @return 预警任务渠道
     */
    public AlarmTaskChannel selectAlarmTaskChannelById(Long id);

    /**
     * 查询预警任务渠道列表
     *
     * @param alarmTaskChannel 预警任务渠道
     * @return 预警任务渠道集合
     */
    public List<AlarmTaskChannel> selectAlarmTaskChannelList(AlarmTaskChannel alarmTaskChannel);

    /**
     * 根据任务ID查询渠道列表
     *
     * @param taskId 任务ID
     * @return 渠道列表
     */
    public List<AlarmTaskChannel> selectAlarmTaskChannelsByTaskId(Long taskId);

    /**
     * 新增预警任务渠道
     *
     * @param alarmTaskChannel 预警任务渠道
     * @return 结果
     */
    public int insertAlarmTaskChannel(AlarmTaskChannel alarmTaskChannel);

    /**
     * 修改预警任务渠道
     *
     * @param alarmTaskChannel 预警任务渠道
     * @return 结果
     */
    public int updateAlarmTaskChannel(AlarmTaskChannel alarmTaskChannel);

    /**
     * 批量删除预警任务渠道
     *
     * @param ids 需要删除的预警任务渠道ID
     * @return 结果
     */
    public int deleteAlarmTaskChannelByIds(Long[] ids);

    /**
     * 删除预警任务渠道
     *
     * @param id 预警任务渠道ID
     * @return 结果
     */
    public int deleteAlarmTaskChannelById(Long id);

    /**
     * 根据任务ID删除渠道
     *
     * @param taskId 任务ID
     * @return 结果
     */
    public int deleteAlarmTaskChannelByTaskId(Long taskId);
}
